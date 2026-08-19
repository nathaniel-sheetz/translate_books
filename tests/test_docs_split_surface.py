"""``docs/CHAPTER_DETECTION_GUIDE.md`` must describe the registry that ships.

The guide opens by promising the reader that "everything below is read from
that file, so it stays true as patterns are added" — a claim nothing enforced.
The pattern table, the ``detection_order`` chain, the two generalist patterns
gated on ``detect_min_ratio``, and the front/back/drop-matter keyword lists are
all verbatim restatements of ``src/split_patterns.json``.

Chapter detection is the first thing that has to go right — every later stage
is scoped per chapter — so a reader picking a pattern from a stale table is
choosing wrong at the most expensive point in the pipeline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = (REPO_ROOT / "docs" / "CHAPTER_DETECTION_GUIDE.md").read_text(encoding="utf-8")
REGISTRY = json.loads((REPO_ROOT / "src" / "split_patterns.json").read_text(encoding="utf-8"))


def _table_after(heading: str, doc: str = DOC) -> list[str]:
    """Rows of the first markdown table following ``heading``."""
    section = doc.split(heading, 1)
    assert len(section) == 2, f"anchor {heading!r} not found; the doc moved"
    for block in section[1].split("\n\n"):
        if block.lstrip().startswith("|"):
            return [r for r in block.splitlines() if r.startswith("|") and not r.startswith("|---")]
    raise AssertionError(f"no table found after {heading!r}")


def test_pattern_table_matches_the_registry():
    """Every shipped pattern has a row, and every row is a shipped pattern."""
    rows = _table_after("## Shipped patterns")
    documented = set()
    for row in rows[1:]:  # skip the header row
        cell = row.split("|")[1].strip()
        match = re.fullmatch(r"`([a-z_]+)`", cell)
        if match:
            documented.add(match.group(1))

    actual = set(REGISTRY["patterns"])
    assert documented == actual, (
        "the shipped-patterns table disagrees with src/split_patterns.json.\n"
        f"  documented but not shipped: {sorted(documented - actual)}\n"
        f"  shipped but undocumented:   {sorted(actual - documented)}"
    )


def test_documented_numbering_matches_each_pattern():
    """The table's Numbering column is read straight off the registry entry."""
    rows = _table_after("## Shipped patterns")
    mismatches = []
    for row in rows[1:]:
        cells = [c.strip() for c in row.split("|")[1:-1]]
        match = re.fullmatch(r"`([a-z_]+)`", cells[0])
        if not match:
            continue
        name = match.group(1)
        documented = cells[-1]
        actual = REGISTRY["patterns"][name].get("numbering")
        if documented != actual:
            mismatches.append(f"{name}: doc says {documented!r}, registry says {actual!r}")
    assert not mismatches, "numbering column is stale:\n  " + "\n  ".join(mismatches)


def test_detection_order_matches_the_registry():
    """The prose spells the chain out with arrows; it must match the JSON list.

    Only the arrow-joined run is harvested. The sentence that follows it names
    ``chapter_roman_titled`` and ``roman`` again while explaining *why* the
    titled variants sort first, so reading the whole paragraph would pick up
    prose as though it were ordering.
    """
    section = DOC.split("`detection_order` in the registry decides", 1)
    assert len(section) == 2, "the detection-order paragraph moved"
    para = section[1].split("\n\n", 1)[0]

    chain = re.search(r"(`[a-z_]+`(?:\s*→\s*`[a-z_]+`)+)", para)
    assert chain, "no `a` → `b` → ... chain found in the detection-order paragraph"
    documented = re.findall(r"`([a-z_]+)`", chain.group(1))

    assert documented == REGISTRY["detection_order"], (
        f"doc order {documented} != registry order {REGISTRY['detection_order']}"
    )


def test_the_generalist_patterns_named_in_the_doc_are_the_gated_ones():
    """``allcaps_heading`` and ``bare_roman`` carry detect_min_ratio 0.5.

    The doc explains *why* those two need proof before selection. If a third
    pattern gains a ratio, or one of these loses it, the explanation is wrong.
    """
    gated = {
        name: entry["detect_min_ratio"]
        for name, entry in REGISTRY["patterns"].items()
        if entry.get("detect_min_ratio") is not None
    }
    assert gated == {"allcaps_heading": 0.5, "bare_roman": 0.5}

    section = DOC.split("Two patterns carry a `detect_min_ratio` of", 1)
    assert len(section) == 2, "the detect_min_ratio paragraph moved"
    para = section[1].split("\n\n", 1)[0]
    for name in gated:
        assert f"`{name}`" in para, f"{name} is gated but the paragraph does not name it"
    assert "0.5" in para


def test_chapter_pattern_choices_cover_registry_plus_meta_values():
    """``--chapter-pattern`` offers every registry pattern plus auto/headings/custom.

    The doc lists the six patterns and then "three meta-values". That count was
    wrong on the branch that introduced it (it said two while listing three),
    which is exactly the sort of slip this pins.
    """
    from scripts.harness import _build_parser
    import argparse

    parser = _build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    split_preview = sub.choices["split-preview"]
    action = next(a for a in split_preview._actions if "--chapter-pattern" in a.option_strings)

    meta = {"auto", "headings", "custom"}
    assert set(action.choices) == set(REGISTRY["patterns"]) | meta

    section = DOC.split("meta-values accepted by the `--chapter-pattern` flag:", 1)
    assert len(section) == 2, "the meta-values paragraph moved"
    para = section[1].split("\n\n---", 1)[0]
    for value in meta:
        assert f"**`{value}`**" in para, f"meta-value {value} is offered but undocumented"


def test_front_back_and_drop_matter_headings_match_the_registry():
    """The three keyword tables restate the registry's regex lists.

    Compares on the human-readable heading each regex recognizes, since the doc
    lists "Preface, Foreword, ..." rather than raw patterns.
    """
    checks = [
        ("**Front matter**", "front_matter_patterns"),
        ("**Back matter**", "back_matter_patterns"),
        ("**Dropped**", "drop_matter_patterns"),
    ]
    rows = _table_after("The registry also classifies non-chapter sections by heading text:")
    by_label = {r.split("|")[1].strip(): r.split("|")[2].strip() for r in rows}

    for label, key in checks:
        assert label in by_label, f"{label} row missing from the classification table"
        documented = [h.strip().lower() for h in by_label[label].split(",")]
        patterns = REGISTRY[key]
        assert len(documented) == len(patterns), (
            f"{label}: doc lists {len(documented)} headings, registry has {len(patterns)}"
        )
        # Every documented heading must be matched by some registry pattern.
        unmatched = [
            h for h in documented
            if not any(re.match(p, h, re.IGNORECASE) for p in patterns)
        ]
        assert not unmatched, f"{label}: no registry pattern matches {unmatched}"
