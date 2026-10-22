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

#: Optional per-book sidecar holding rules specific to one book, with stable
#: ids. It is a sidecar rather than a reshape of ``style.json`` because that file
#: is one free-text blob across every book in ``projects/``, and tiering its
#: schema would mean regenerating all of them. Since the house set below is
#: merged in for every book, a book without the sidecar still cites rule ids —
#: the sidecar adds only what is specific to that book.
STYLE_RULES_FILENAME = "style_rules.json"

#: The house rules every book is held to, shipped with the repo rather than
#: copied into each book. A house standard lives in ``prompts/`` because that is
#: where what the models are told lives.
#:
#: Per-user like the other prompts: the operator's own copy is gitignored and the
#: ``.example`` twin is what a fresh clone has. Keep ids and wording as shipped
#: unless you mean to diverge — ``Issue.rule_id`` is the key rule suppressions and
#: per-rule precision are computed on, so two installs disagreeing about what an
#: id means would corrupt both.
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
HOUSE_STYLE_RULES_FILE = _PROMPTS_DIR / "house_style_rules.json"
HOUSE_STYLE_RULES_EXAMPLE_FILE = _PROMPTS_DIR / "house_style_rules.example.json"


#: How a house rule is enforced (its optional ``enforce`` field).
#:
#: - ``prompt`` (default): the translator and the judges read it. With no
#:   ``triggers`` it is global and sits in the translator's cached prefix; with
#:   ``triggers`` it reaches only the chunks whose English sets one off.
#: - ``mechanical``: a named fixer (:mod:`src.house.mechanical`) applies it at
#:   chunk commit and logs each change, so no prompt carries it.
#: - ``draft``: a house default for the drafting steps only. It pre-answers the
#:   questions it ``settles`` (style guide, address map, decisions stage) and
#:   reaches no translator or judge prompt.
ENFORCE_PROMPT = "prompt"
ENFORCE_MECHANICAL = "mechanical"
ENFORCE_DRAFT = "draft"
ENFORCE_MODES = (ENFORCE_PROMPT, ENFORCE_MECHANICAL, ENFORCE_DRAFT)

#: Who a rule list is being built for. The translator and the judges get the
#: ``prompt`` rules; the decisions stage gets every rule in scope.
AUDIENCE_TRANSLATOR = "translator"
AUDIENCE_JUDGE = "judge"
AUDIENCE_DECISIONS = "decisions"


def rule_enforce(rule: dict[str, Any]) -> str:
    """The rule's ``enforce`` mode; an unknown value reads as ``prompt``."""
    mode = str(rule.get("enforce") or ENFORCE_PROMPT).strip()
    return mode if mode in ENFORCE_MODES else ENFORCE_PROMPT


def rule_triggers(rule: dict[str, Any]) -> list[str]:
    """The English source forms that bring a rule into a chunk; ``[]`` means global."""
    raw = rule.get("triggers")
    if isinstance(raw, str):
        raw = [raw]
    return [str(t) for t in raw or [] if str(t).strip()]


def rule_settles(rule: dict[str, Any]) -> dict[str, Any]:
    """``{"<step>:<question>": answer}`` for the questions this rule answers.

    Keys share one space across the steps: ``style:<question id>`` for the
    style-guide questionnaire, ``decisions:<taxonomy category>`` for the
    decisions stage, ``address:global_rules`` for the address map.
    """
    raw = rule.get("settles")
    return {str(k): v for k, v in raw.items()} if isinstance(raw, dict) else {}


def _house_rules_path() -> Path:
    """The operator's house rules if present, else the checked-in example.

    Mirrors the per-user prompt convention in
    ``style_guide_wizard._resolve_prompt_path``, resolved locally for the same
    reason ``text_utils._resolve_dialogue_path`` does rather than importing it:
    judges, triage and the audit panel all import this module, and reaching into
    the style-guide wizard would pull the setup tooling — and a private name —
    into every one of those runs for an eight-line lookup.

    Returns the user path when neither exists, so the caller reports the name the
    operator would expect to create.
    """
    if HOUSE_STYLE_RULES_FILE.exists():
        return HOUSE_STYLE_RULES_FILE
    if HOUSE_STYLE_RULES_EXAMPLE_FILE.exists():
        return HOUSE_STYLE_RULES_EXAMPLE_FILE
    return HOUSE_STYLE_RULES_FILE

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


def _read_house_rules(path: Path) -> Optional[list[dict[str, Any]]]:
    """The ``every_book`` rules in one file, or ``None`` when it is unusable.

    ``None`` rather than ``[]`` so the caller can tell "this file is broken"
    from "this file records no house rules", and fall back only on the first.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Ignoring unreadable %s: %s", path, exc)
        return None
    rules = data.get("every_book") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        logger.warning("Ignoring %s: expected an 'every_book' list", path)
        return None
    return [rule for rule in rules if isinstance(rule, dict)]


def load_house_rules() -> list[dict[str, Any]]:
    """The ``every_book`` rules every book is held to, or ``[]``.

    ``where_the_guide_agrees`` is deliberately not returned. Those rules are
    adopted one book at a time, by copying the rule into that book's sidecar;
    injecting them everywhere would hold a book to a rule its style guide never
    agreed to, which is the condition that section is named for.

    Reads the operator's copy when present and the checked-in example otherwise,
    so a clone that was never hand-primed with ``cp`` still judges against the
    house standard instead of nothing.

    A *broken* operator copy falls back to the example too, not to nothing. An
    absent override already degraded to the example, so a JSON typo in one was
    the only way left to have every book judged against no rules at all —
    silently, across triage, the editorial judge and the audit panel at once.
    """
    path = _house_rules_path()
    rules = _read_house_rules(path)
    if rules is None and path != HOUSE_STYLE_RULES_EXAMPLE_FILE:
        logger.warning(
            "Falling back to %s for the house rules", HOUSE_STYLE_RULES_EXAMPLE_FILE
        )
        rules = _read_house_rules(HOUSE_STYLE_RULES_EXAMPLE_FILE)
    return rules or []


def _read_book_sidecar(project_dir: Path) -> list[dict[str, Any]]:
    """Every entry in the book's sidecar: its own rules and its ``off`` overrides."""
    path = Path(project_dir) / STYLE_RULES_FILENAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Ignoring unreadable %s: %s", path, exc)
        return []
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        logger.warning("Ignoring %s: expected a 'rules' list", path)
        return []
    return [rule for rule in rules if isinstance(rule, dict)]


def load_book_rules(project_dir: Path) -> list[dict[str, Any]]:
    """This book's own sidecar rules, or ``[]`` when it has none.

    An ``{"id": ..., "off": true}`` entry is an override of a house rule, not a
    rule of the book's own, and is left out (:func:`load_book_overrides`).

    A malformed sidecar is logged and treated as absent rather than raised, and
    the house rules still reach the judge: one unreadable file must not cost a
    book the rules it shares with every other book.
    """
    return [rule for rule in _read_book_sidecar(project_dir) if not rule.get("off")]


def load_book_overrides(project_dir: Optional[Path]) -> dict[str, dict[str, Any]]:
    """``{rule id: entry}`` for the house rules this book has turned off.

    Written when the user answers a house-settled question differently for one
    book: the rule then drops out of that book's translator prefix and judge
    context, and the book's own answer goes into its style guide instead.
    """
    if project_dir is None:
        return {}
    return {
        str(rule.get("id")).strip(): rule
        for rule in _read_book_sidecar(project_dir)
        if rule.get("off") and str(rule.get("id") or "").strip()
    }


def set_book_override(
    project_dir: Path, rule_id: str, *, off: bool = True, reason: str = ""
) -> bool:
    """Turn a house rule off (or back on) for one book. Returns whether anything changed.

    Keeps the sidecar's other entries and its top-level shape as they were.
    """
    path = Path(project_dir) / STYLE_RULES_FILENAME
    doc: Any = {"rules": []}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"{path} is unreadable ({exc}); fix it before overriding a rule") from exc
    rules = doc.get("rules") if isinstance(doc, dict) else doc
    if not isinstance(rules, list):
        raise ValueError(f"{path}: expected a 'rules' list")
    present = [r for r in rules if isinstance(r, dict) and r.get("off") and r.get("id") == rule_id]
    if off and present:
        return False
    if not off and not present:
        return False
    if off:
        entry: dict[str, Any] = {"id": rule_id, "off": True}
        if reason:
            entry["reason"] = reason
        rules.append(entry)
    else:
        rules[:] = [r for r in rules if r not in present]
    if isinstance(doc, list):
        doc = {"rules": rules}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def house_rules_for_book(project_dir: Optional[Path]) -> list[dict[str, Any]]:
    """The house rules in scope for this book, minus the ones it turned off.

    Scope is ``every_book`` or ``group:<id>`` (:mod:`src.house.groups`); with no
    ``project_dir`` only the every-book rules apply. Every ``enforce`` mode is
    kept; :func:`rules_for` narrows by audience.
    """
    from src.house import book_slug, in_scope
    from src.house.groups import group_ids_of

    groups = group_ids_of(book_slug(project_dir))
    off = load_book_overrides(project_dir)
    return [
        rule for rule in load_house_rules()
        if in_scope(rule.get("scope"), groups) and str(rule.get("id") or "").strip() not in off
    ]


def rules_for(
    project_dir: Optional[Path],
    source_text: Optional[str] = None,
    *,
    audience: str = AUDIENCE_JUDGE,
) -> list[dict[str, Any]]:
    """The rules one prompt about this book should carry.

    Applies, in order: scope (the book's groups), the book's ``off`` overrides,
    the audience, and the triggers. The judges also get the book's own sidecar
    rules after the house set; the translator gets the house set only, because
    its book-specific rules are its style guide.

    ``source_text`` is the English the prompt is about (a chunk, or the whole
    book for a book-level judge prompt). A triggered rule stays only when one of
    its triggers occurs in it, matched like a glossary term
    (:func:`src.house.match.trigger_matches`). ``None`` keeps every triggered
    rule, for a caller that cannot say what the prompt covers.
    """
    from src.house.match import any_trigger

    rules = house_rules_for_book(project_dir)
    if audience != AUDIENCE_DECISIONS:
        rules = [r for r in rules if rule_enforce(r) == ENFORCE_PROMPT]
    if audience == AUDIENCE_JUDGE and project_dir is not None:
        rules = merge_style_rules(rules, load_book_rules(project_dir))
    if source_text is not None:
        rules = [
            r for r in rules
            if not rule_triggers(r) or any_trigger(rule_triggers(r), source_text)
        ]
    return rules


def split_global_triggered(
    rules: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(global, triggered)``: rules with no triggers, and rules with some."""
    return (
        [r for r in rules if not rule_triggers(r)],
        [r for r in rules if rule_triggers(r)],
    )


def settled_by_house(project_dir: Optional[Path], step: str) -> dict[str, dict[str, Any]]:
    """``{question: {answer, rule_id}}`` for every ``<step>:<question>`` a house rule settles.

    Only rules in scope and not turned off for this book count. The first rule
    to settle a question wins; a later one that disagrees is reported in
    ``conflicts`` so the gate can show it rather than pick silently.
    """
    prefix = f"{step}:"
    settled: dict[str, dict[str, Any]] = {}
    for rule in house_rules_for_book(project_dir):
        rule_id = str(rule.get("id") or "").strip()
        for key, answer in rule_settles(rule).items():
            if not key.startswith(prefix):
                continue
            question = key[len(prefix):]
            prior = settled.get(question)
            if prior is None:
                settled[question] = {"answer": answer, "rule_id": rule_id, "conflicts": []}
            elif prior["answer"] != answer:
                prior["conflicts"].append({"answer": answer, "rule_id": rule_id})
    return settled


def has_book_rules(project_dir: Path) -> bool:
    """Whether this book adds rules of its own to the house set.

    Once the house rules reach every book, "does this book have style rules" is
    always yes and reports nothing. What a manifest can still say is whether the
    book adds anything, so that is what this answers.
    """
    return bool(load_book_rules(project_dir))


def merge_style_rules(
    house: list[dict[str, Any]], book: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """House rules first, then the book's own, deduped by ``id``.

    On a collision the house wording wins and the book's ``note`` overrides it.
    That is the house file's own contract: ids and wording stay identical across
    books so findings aggregate by rule, while a book's note may vary.
    """
    merged: list[dict[str, Any]] = []
    index_of: dict[str, int] = {}
    for rule in house:
        rule_id = str(rule.get("id") or "").strip()
        if not rule_id or rule_id in index_of:
            continue
        index_of[rule_id] = len(merged)
        merged.append(dict(rule))
    for rule in book:
        rule_id = str(rule.get("id") or "").strip()
        if not rule_id:
            continue
        at = index_of.get(rule_id)
        if at is None:
            index_of[rule_id] = len(merged)
            merged.append(dict(rule))
            continue
        note = str(rule.get("note") or "").strip()
        if note:
            merged[at]["note"] = note
    return merged


def load_style_rules(project_dir: Path) -> str:
    """The rules this book is judged against: the house set plus its own.

    Every book gets the house rules in its scope whether or not it has a
    sidecar, so a judge can always cite a rule id. Mechanical and draft-only
    rules are left out (a fixer already applied the one, and the other binds
    the drafting steps), and so is a triggered rule whose triggers never occur
    in the book's source: the judge prompt is book-level, so the book is what a
    trigger is matched against. Empty only when nothing is left.
    """
    from src.house import read_source

    return format_style_rules(
        rules_for(project_dir, read_source(project_dir), audience=AUDIENCE_JUDGE)
    )


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
    telling the judge not to report something nobody objects to. A chunk edited
    since its evaluators ran is *not* skipped: the coded finding it holds is
    still worth de-duplicating against, and dropping it here reintroduced the
    double-reporting this function exists to prevent.

    Without this the dictionary and grammar evaluators (3.2 and 2.8 findings per
    chunk) and the editorial judge report the same defect independently, the
    Review tab counts it twice, and the badge inflates.
    """
    from web_ui.evaluations import (  # local import: web_ui is the persistence layer
        REVIEW_CODED_TYPES,
        build_dismissed,
        build_triaged,
        is_dismissed,
        is_ignored,
        is_triaged,
        load_all_feedback_by_chunk,
        load_all_triage_by_chunk,
        load_project_ignored_terms,
    )

    project_dir = Path(project_dir)
    evaluations_dir = project_dir / "evaluations"
    if not evaluations_dir.exists():
        return {}

    ignored = load_project_ignored_terms(project_dir)
    feedback_by_chunk = load_all_feedback_by_chunk(project_dir)
    triage_by_chunk = load_all_triage_by_chunk(project_dir)
    coded: dict[str, list[str]] = {}

    for path in sorted(evaluations_dir.glob("*.json")):
        chunk_id = path.stem
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable evaluation %s: %s", path, exc)
            continue
        if not isinstance(payload, dict):
            continue

        by_key, by_index = build_dismissed(feedback_by_chunk.get(chunk_id, []))
        tr_by_key = build_triaged(triage_by_chunk.get(chunk_id, []))
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
                if is_triaged(tr_by_key, eval_name, issue):
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
      * ``address_map`` — ``address_map.json`` rendered for the forms-of-address
        judge: prose ``content``, ``global_rules`` and one line per pair direction.
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

            from src.judges.address_judge import render_address_map

            amap = load_address_map(map_path)
            # Prose, global rules and every pair: the prose alone is a summary
            # and need not name the pairs the judge has to check against.
            prose = render_address_map(amap)
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
    "has_book_rules",
    "house_rules_for_book",
    "load_book_overrides",
    "load_book_rules",
    "load_calibration_examples",
    "load_coded_findings",
    "load_house_rules",
    "load_style_rules",
    "merge_style_rules",
    "rule_enforce",
    "rule_settles",
    "rule_triggers",
    "rules_for",
    "set_book_override",
    "settled_by_house",
    "split_global_triggered",
]
