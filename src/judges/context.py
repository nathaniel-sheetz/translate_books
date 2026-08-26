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

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Optional per-book sidecar holding the style guide's hard rules broken out with
#: stable ids. It is a sidecar rather than a reshape of ``style.json`` because
#: that file is one free-text blob across every book in ``projects/``, and
#: tiering its schema would mean regenerating all of them. A book without the
#: sidecar still judges — it just cannot cite rule ids.
STYLE_RULES_FILENAME = "style_rules.json"

#: Accepted/dismissed examples, generated from the human feedback corpus by
#: ``scripts/editorial_metrics.py --write-examples``.
CALIBRATION_EXAMPLES_FILENAME = "editorial_examples.txt"

#: How many of a chunk's coded findings to show the editorial judge. The
#: dictionary and grammar evaluators run 3.2 and 2.8 findings per chunk, and a
#: pathological chunk can carry dozens; the list is a do-not-repeat hint, so it
#: is capped rather than allowed to crowd out the passage itself.
MAX_CODED_FINDINGS_PER_CHUNK = 25


def format_style_rules(rules: list[dict[str, Any]]) -> str:
    """Render the ``style_rules.json`` entries as an id-cited rule list."""
    lines: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_id = str(rule.get("id") or "").strip()
        text = str(rule.get("rule") or rule.get("text") or "").strip()
        if not rule_id or not text:
            continue
        note = str(rule.get("note") or "").strip()
        lines.append(f'- "{rule_id}": {text}' + (f" ({note})" if note else ""))
    return "\n".join(lines)


def load_style_rules(project_dir: Path) -> str:
    """Load the optional hard-rule sidecar, or an empty string if absent.

    A malformed sidecar is logged and treated as absent rather than raised: the
    judge degrades to un-cited STYLE_GUIDE findings, which is a lesser failure
    than refusing to review the book.
    """
    path = Path(project_dir) / STYLE_RULES_FILENAME
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Ignoring unreadable %s: %s", path, exc)
        return ""
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        logger.warning("Ignoring %s: expected a 'rules' list", path)
        return ""
    return format_style_rules(rules)


def load_calibration_examples(project_dir: Path) -> str:
    """Load the per-book calibration examples, or an empty string if absent."""
    path = Path(project_dir) / CALIBRATION_EXAMPLES_FILENAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_coded_findings(project_dir: Path) -> dict[str, list[str]]:
    """Live coded-evaluator findings per chunk, as do-not-repeat lines.

    "Live" means what the reader would currently show: dismissed findings and
    findings naming an ignored term are excluded, because the human has already
    said those are not defects and re-suppressing them via this list would be
    telling the judge not to report something nobody objects to. Stale
    evaluations are skipped for the same reason the reader skips them — they
    describe prose that has since changed.

    Without this the dictionary and grammar evaluators (3.2 and 2.8 findings per
    chunk) and the editorial judge report the same defect independently, the
    Review tab counts it twice, and the badge inflates.
    """
    from web_ui.evaluations import (  # local import: web_ui is the persistence layer
        REVIEW_CODED_TYPES,
        build_dismissed,
        is_dismissed,
        is_ignored,
        load_all_feedback_by_chunk,
        load_project_ignored_terms,
    )

    project_dir = Path(project_dir)
    evaluations_dir = project_dir / "evaluations"
    if not evaluations_dir.exists():
        return {}

    ignored = load_project_ignored_terms(project_dir)
    feedback_by_chunk = load_all_feedback_by_chunk(project_dir)
    coded: dict[str, list[str]] = {}

    for path in sorted(evaluations_dir.glob("*.json")):
        chunk_id = path.stem
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable evaluation %s: %s", path, exc)
            continue
        if not isinstance(payload, dict) or payload.get("stale"):
            continue

        by_key, by_index = build_dismissed(feedback_by_chunk.get(chunk_id, []))
        lines: list[str] = []
        for result in payload.get("results") or []:
            if not isinstance(result, dict):
                continue
            eval_name = result.get("eval_name")
            if eval_name not in REVIEW_CODED_TYPES:
                continue
            for index, issue in enumerate(result.get("issues") or []):
                if not isinstance(issue, dict):
                    continue
                if is_dismissed(by_key, by_index, eval_name, index, issue):
                    continue
                if is_ignored(ignored, eval_name, issue):
                    continue
                message = str(issue.get("message") or "").strip()
                if message:
                    lines.append(f"[{eval_name}] {message}")
        if lines:
            coded[chunk_id] = lines[:MAX_CODED_FINDINGS_PER_CHUNK]

    return coded


def _add_editorial_inputs(
    project_dir: Path, context: dict, style_path: Path
) -> None:
    """Populate the editorial judge's book-level inputs on ``context``.

    Every input here is optional. A book with no style guide, no glossary and no
    rule sidecar still judges — the prompt carries a stated placeholder for each
    — because the categories that need none of them (GRAMMAR, NATURALNESS,
    CONSISTENCY, FIDELITY_SUSPECT) are four of the five.
    """
    from src.utils.file_io import format_glossary_for_prompt, load_glossary, load_style_guide

    if style_path.exists():
        try:
            context["style_guide"] = load_style_guide(style_path).content
        except Exception as exc:  # noqa: BLE001 - an unusable style guide is not fatal
            logger.warning("Ignoring unreadable %s: %s", style_path, exc)

    glossary_path = project_dir / "glossary.json"
    if glossary_path.exists():
        try:
            context["glossary"] = format_glossary_for_prompt(load_glossary(glossary_path))
        except Exception as exc:  # noqa: BLE001 - same
            logger.warning("Ignoring unreadable %s: %s", glossary_path, exc)

    style_rules = load_style_rules(project_dir)
    if style_rules:
        context["style_rules"] = style_rules

    examples = load_calibration_examples(project_dir)
    if examples:
        context["calibration_examples"] = examples

    try:
        context["coded_findings"] = load_coded_findings(project_dir)
    except Exception as exc:  # noqa: BLE001 - dedup is a nicety, not a prerequisite
        logger.warning("Could not load coded findings for %s: %s", project_dir, exc)


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
      * ``style_guide`` / ``style_rules`` / ``glossary`` /
        ``calibration_examples`` / ``coded_findings`` — for the editorial judge.
        Loaded only when it is in ``judge_names``: the glossary and the coded
        findings walk cost real I/O, and a dialogue-only wave has no use for
        either.

    Returns ``(context, error)``. ``error`` is a human-readable string when the
    ``address`` judge is requested but no usable ``address_map.json`` exists
    (the caller emits it and refuses to run); otherwise ``None``.
    """
    project_dir = Path(project_dir)
    context: dict = {"judge_model": model, "judge_provider": provider}

    style_path = project_dir / "style.json"
    if style_path.exists():
        context["style_json_path"] = style_path

    if "editorial" in judge_names:
        _add_editorial_inputs(project_dir, context, style_path)

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


__all__ = [
    "build_judge_context",
    "format_style_rules",
    "load_calibration_examples",
    "load_coded_findings",
    "load_style_rules",
]
