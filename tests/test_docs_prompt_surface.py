"""``docs/PROMPT_GUIDE.md`` must describe the prompt set that actually ships.

Two enumerations in that document are load-bearing and silently rot:

* the "overridable six" table, which claims to be "exactly as listed in
  ``.gitignore``" — the per-user prompt convention. A seventh per-user prompt
  added to ``.gitignore`` without a table row leaves users unaware they can
  own it; a table row without a ``.gitignore`` line means their private copy
  gets committed. ``prompts/glossary_bootstrap_word.example.txt`` already sits
  in that second gap (see TODOS.md).
* the feature-detector table, which enumerates every entry in
  ``text_feature_detector.DETECTORS``. A new detector changes which
  style-guide questions users are asked, so shipping one undocumented is a
  user-visible behavior change with no paper trail.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.text_feature_detector import DETECTORS

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "prompts"
DOC = (REPO_ROOT / "docs" / "PROMPT_GUIDE.md").read_text(encoding="utf-8")


def _gitignored_prompts() -> set[str]:
    """Prompt basenames ``.gitignore`` reserves as per-user copies.

    Only ``prompts/<file>.<ext>`` lines count — directory rules
    (``prompts/history/``) and negations are not per-user prompt slots.
    """
    body = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    out = set()
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("prompts/") or line.startswith("!") or line.endswith("/"):
            continue
        name = line[len("prompts/"):]
        if "/" in name or "." not in name:
            continue
        out.add(name)
    return out


def _documented_overridable() -> set[str]:
    """Basenames in the 'Your copy (gitignored)' column of the overridable table."""
    section = DOC.split("The overridable six, exactly as listed in", 1)
    assert len(section) == 2, "the overridable-prompts table moved; update this anchor"
    table = section[1].split("\n\n", 2)[1]

    out = set()
    for row in table.splitlines():
        if not row.startswith("|") or row.startswith("|---"):
            continue
        first = row.split("|")[1]
        # `prompts/style_guide_questions.txt` / `.json` -> both extensions
        names = re.findall(r"`(prompts/[A-Za-z_0-9.]+|\.[a-z]+)`", first)
        if not names:
            continue
        stem = None
        for name in names:
            if name.startswith("prompts/"):
                stem = name[len("prompts/"):]
                out.add(stem)
            elif stem:  # a bare `.json` continuation of the previous entry
                out.add(Path(stem).stem + name)
    return out


def test_overridable_table_matches_gitignore():
    """The doc's claim "exactly as listed in .gitignore" must stay literally true."""
    documented = _documented_overridable()
    ignored = _gitignored_prompts()
    assert documented, "no rows parsed out of the overridable table — the pattern went stale"
    assert documented == ignored, (
        "PROMPT_GUIDE.md's overridable table disagrees with .gitignore.\n"
        f"  in the doc but not gitignored: {sorted(documented - ignored)}\n"
        f"  gitignored but undocumented:   {sorted(ignored - documented)}"
    )


def test_every_overridable_prompt_ships_an_example():
    """A per-user prompt is unusable on a fresh clone without its committed default."""
    missing = []
    for name in _gitignored_prompts():
        p = Path(name)
        example = PROMPTS_DIR / f"{p.stem}.example{p.suffix}"
        if not example.exists():
            missing.append(example.name)
    assert not missing, f"gitignored prompts with no .example twin: {sorted(missing)}"


def test_every_example_prompt_is_documented_somewhere():
    """A shipped ``*.example.*`` the guide never names is an undiscoverable knob.

    Deliberately weaker than the table check: the word-variant bootstrap prompt
    is named in the "Setup beats" table rather than the overridable one,
    because it has no gitignored slot yet.
    """
    undocumented = []
    for example in sorted(PROMPTS_DIR.glob("*.example.*")):
        stem = example.name.replace(".example", "")
        if example.name not in DOC and stem not in DOC:
            undocumented.append(example.name)
    assert not undocumented, f"PROMPT_GUIDE.md never mentions: {undocumented}"


def test_feature_detector_table_matches_the_registry():
    """The 15-row detector table is the registry; adding a detector must update it."""
    section = DOC.split("### Detector library", 1)
    assert len(section) == 2, "the detector table moved; update this anchor"
    table = section[1].split("\n\n", 2)[1]

    documented = set()
    for row in table.splitlines():
        if not row.startswith("|") or row.startswith("|---"):
            continue
        cell = row.split("|")[1].strip()
        match = re.fullmatch(r"`([a-z_]+)`", cell)
        if match:
            documented.add(match.group(1))

    actual = set(DETECTORS)
    assert documented == actual, (
        "PROMPT_GUIDE.md's detector table disagrees with text_feature_detector.DETECTORS.\n"
        f"  documented but not registered: {sorted(documented - actual)}\n"
        f"  registered but undocumented:   {sorted(actual - documented)}"
    )
