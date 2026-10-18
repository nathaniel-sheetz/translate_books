"""
Pre-answering a book's questions from what the house already knows.

Order of sources, first wins:

1. an explicit answer the book already has;
2. a house rule that ``settles`` the question;
3. the series siblings' most common answer;
4. the domain siblings' most common answer;
5. the setup locale (``dialect`` only) or feature detection.

Every other source that answered differently is listed in ``divergence``, so
the gate shows the disagreement beside the question instead of hiding it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from src.house.groups import DOMAIN, SERIES
from src.house.precedent import normalize_answer, style_votes


def _rule_source(rule_id: str) -> str:
    return f"house rule `{rule_id}`"


def style_prefills(
    project_dir: Path,
    questions: list[dict],
    *,
    locale: str = "",
    explicit: Optional[dict[str, Any]] = None,
    precedent: Optional[dict] = None,
) -> dict[str, dict[str, Any]]:
    """``{question id: {prefilled, prefilled_reason, source, rule_id?, divergence}}``.

    A question nothing pre-answers is absent. ``source`` is one of ``book``,
    ``house_rule``, ``series``, ``domain``, ``locale``; ``rule_id`` is set when
    a house rule answered, whether it won or only disagreed.
    """
    from src.judges.context import settled_by_house
    from src.style_guide_wizard import dialect_id_from_locale

    explicit = explicit or {}
    settled = settled_by_house(project_dir, "style")
    precedent = precedent or {}
    out: dict[str, dict[str, Any]] = {}

    for q in questions:
        qid = q.get("id")
        if not qid:
            continue
        candidates: list[dict[str, Any]] = []
        if qid in explicit:
            key, label = normalize_answer(q, explicit[qid])
            candidates.append({"answer": key, "label": label, "source": "book",
                               "reason": "this book's own answer"})
        if qid in settled:
            hit = settled[qid]
            key, label = normalize_answer(q, hit["answer"])
            candidates.append({"answer": key, "label": label, "source": "house_rule",
                               "rule_id": hit["rule_id"], "reason": _rule_source(hit["rule_id"])})
            for other in hit["conflicts"]:
                key, label = normalize_answer(q, other["answer"])
                candidates.append({"answer": key, "label": label, "source": "house_rule",
                                   "rule_id": other["rule_id"],
                                   "reason": _rule_source(other["rule_id"])})
        for kind in (SERIES, DOMAIN):
            votes = style_votes(precedent, qid, kind)
            total = sum(len(v["slugs"]) for v in votes)
            for v in votes:
                candidates.append({
                    "answer": v["answer"], "label": v["label"], "source": kind,
                    "reason": f"{len(v['slugs'])} of {total} {kind} siblings "
                              f"({', '.join(v['slugs'])})",
                })
        if qid == "dialect" and locale:
            mapped = dialect_id_from_locale(locale, q)
            if mapped:
                candidates.append({"answer": mapped, "label": mapped, "source": "locale",
                                   "reason": f"from setup locale {locale!r}"})
        if not candidates:
            continue
        winner = candidates[0]
        divergence = []
        seen = {winner["answer"]}
        for c in candidates[1:]:
            if c["answer"] in seen:
                continue
            seen.add(c["answer"])
            divergence.append({"answer": c["answer"], "label": c["label"],
                               "source": c["source"], "reason": c["reason"]})
        entry = {
            "prefilled": winner["answer"],
            "prefilled_reason": winner["reason"],
            "source": winner["source"],
            "divergence": divergence,
        }
        house_rule = next((c for c in candidates if c["source"] == "house_rule"), None)
        if house_rule:
            entry["rule_id"] = house_rule["rule_id"]
            entry["house_answer"] = house_rule["answer"]
        out[qid] = entry
    return out


def house_covered(project_dir: Path) -> list[dict[str, str]]:
    """Style questions a global prompt rule answers: the translator's prefix already says it.

    The guide drafter is told not to restate these, which keeps guides short.
    A draft-only rule is not here: its answer reaches the translator only
    through the guide, so the guide must carry it.
    """
    from src.judges.context import (
        ENFORCE_PROMPT, house_rules_for_book, rule_enforce, rule_settles, rule_triggers,
    )

    covered = []
    for rule in house_rules_for_book(project_dir):
        if rule_enforce(rule) != ENFORCE_PROMPT or rule_triggers(rule):
            continue
        for key in rule_settles(rule):
            if key.startswith("style:"):
                covered.append({
                    "question": key[len("style:"):],
                    "rule_id": str(rule.get("id") or ""),
                    "rule": str(rule.get("rule") or ""),
                })
    return covered


def style_house_context(project_dir: Path, precedent: Optional[dict] = None) -> str:
    """What the style-guide drafter must know the house already says, or ``""``.

    Two parts: the global house rules the translator reads with every chunk
    (so the guide does not restate them, which keeps guides short), and the
    series siblings' guides as reference. Domain siblings share vocabulary,
    not voice, so their guides are left out.
    """
    from src.house import PROJECTS_DIR
    from src.house.prompt import load_translator_house
    from src.utils.file_io import load_style_guide

    parts: list[str] = []
    house = load_translator_house(project_dir)
    if house.global_rules:
        lines = [f"- {r.get('rule')} (house rule `{r.get('id')}`)" for r in house.global_rules]
        parts.append(
            "HOUSE RULES ALREADY IN THE TRANSLATOR'S PROMPT\n"
            "The translator reads these with every chunk. Do not restate them in the "
            "guide; write only what this book adds or where it departs.\n" + "\n".join(lines)
        )
    if house.triggered:
        lines = [f"- {r.get('rule')} (house rule `{r.get('id')}`)" for r in house.triggered]
        parts.append(
            "HOUSE RULES THE TRANSLATOR GETS WHEN A PASSAGE CALLS FOR THEM\n"
            "Also not to be restated:\n" + "\n".join(lines)
        )
    guides = []
    for sib in (precedent or {}).get("siblings") or []:
        if sib.get("kind") != SERIES:
            continue
        path = PROJECTS_DIR / sib["slug"] / "style.json"
        try:
            content = load_style_guide(path).content.strip()
        except Exception:  # noqa: BLE001 - a sibling without a usable guide adds nothing
            continue
        if content:
            guides.append(f"--- {sib['slug']} ---\n{content}")
    if guides:
        parts.append(
            "STYLE GUIDES OF EARLIER BOOKS IN THIS SERIES (reference)\n"
            "Keep this book consistent with them unless something in it argues "
            "otherwise, and say where it departs.\n\n" + "\n\n".join(guides)
        )
    return "\n\n".join(parts)


def glossary_prefills(
    project_dir: Path, source_text: str, precedent: Optional[dict] = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """``(prefilled, candidates_only, divergence)`` for the glossary step.

    - ``prefilled``: ``{english, translation, alternatives, type, origin, evidence}``
      for every house lexicon entry with a rendering and every sibling glossary
      term whose English occurs in the source, unless the siblings' committed text
      has the word and never their rendering. The lexicon wins over siblings.
    - ``candidates_only``: lexicon entries with no rendering (always raise them).
    - ``divergence``: ``{english, chosen, chosen_origin, other, other_origin}``
      wherever the lexicon and the siblings, or two siblings, disagree.
    """
    from src.house import book_slug
    from src.house.groups import group_ids_of
    from src.house.lexicon import entries_for
    from src.house.precedent import (
        bare, committed_uses, example_of, format_concordance, leading_rendering,
    )

    precedent = precedent or {}
    by_english = precedent.get("glossary") or {}
    prefilled: dict[str, dict[str, Any]] = {}
    candidates_only: list[dict[str, Any]] = []
    divergence: list[dict[str, Any]] = []

    for entry in entries_for(group_ids_of(book_slug(project_dir)), source_text):
        key = entry["english"].casefold()
        if not entry["spanish"]:
            candidates_only.append(entry)
            continue
        prefilled[key] = {
            "english": entry["english"],
            "translation": entry["spanish"],
            "alternatives": entry["alternatives"],
            "type": entry["type_guess"],
            "origin": f"house lexicon ({entry['scope']})",
            "why": entry["why"],
            "evidence": format_concordance(by_english.get(key, {})),
            "example": example_of(by_english.get(key, {})),
        }

    for key, entry in by_english.items():
        lead = leading_rendering(entry)
        if lead is None:
            continue
        # The siblings' prose has the word and never used their own rendering for
        # it: the glossary row was never acted on, so it is no precedent.
        if key not in prefilled and committed_uses(entry) == 0:
            continue
        origin = "siblings: " + ", ".join(lead["slugs"])
        if key in prefilled:
            if bare(prefilled[key]["translation"]).casefold() != lead["spanish"].casefold():
                divergence.append({
                    "english": entry["english"],
                    "chosen": prefilled[key]["translation"],
                    "chosen_origin": prefilled[key]["origin"],
                    "other": lead["spanish"],
                    "other_origin": origin,
                })
            continue
        prefilled[key] = {
            "english": entry["english"],
            "translation": lead["spanish"],
            "alternatives": lead["alternatives"],
            "type": entry.get("type") or "other",
            "origin": origin,
            "why": "",
            "evidence": format_concordance(entry),
            "example": example_of(entry),
        }
        for r in entry["renderings"]:
            if bare(r["spanish"]).casefold() != lead["spanish"].casefold():
                divergence.append({
                    "english": entry["english"],
                    "chosen": lead["spanish"],
                    "chosen_origin": origin,
                    "other": r["spanish"],
                    "other_origin": f"sibling: {r['slug']}",
                })
    return sorted(prefilled.values(), key=lambda p: p["english"].casefold()), candidates_only, divergence


def format_glossary_prefill(
    prefilled: list[dict[str, Any]], divergence: list[dict[str, Any]]
) -> str:
    """The glossary prompt's guidance block for the pre-filled renderings, or ``""``."""
    if not prefilled:
        return ""
    lines = []
    for p in prefilled:
        line = f"- {p['english']} → {p['translation']}"
        if p["alternatives"]:
            line += f" (also: {', '.join(p['alternatives'])})"
        line += f" [{p['origin']}]"
        if p["why"]:
            line += f" — {p['why']}"
        if p["evidence"]:
            line += f" ({p['evidence']})"
        if p.get("example"):
            line += f' e.g. "{p["example"]}"'
        lines.append(line)
    block = (
        "PRE-FILLED RENDERINGS. These come from the house lexicon and from earlier books "
        "in this book's groups. Propose each one with exactly this translation unless "
        "something in this book argues otherwise; if you depart, say why in `context`. "
        "Counts are uses in the earlier books' committed translations. The e.g. sentence is "
        "from an earlier book: check this book uses the word in the same sense.\n" + "\n".join(lines)
    )
    if divergence:
        block += "\n\nWhere the sources disagree (decide, and say which in `context`):\n" + "\n".join(
            f"- {d['english']}: {d['chosen']} [{d['chosen_origin']}] vs {d['other']} [{d['other_origin']}]"
            for d in divergence
        )
    return block


def glossary_departures(
    prefilled: list[dict[str, Any]], committed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pre-filled renderings the committed glossary changed or left out."""
    from src.house.precedent import bare

    by_english = {str(t["english"]).casefold(): t for t in committed}
    out = []
    for p in prefilled:
        term = by_english.get(p["english"].casefold())
        if term is None:
            out.append({"english": p["english"], "prefilled": p["translation"],
                        "committed": None, "origin": p["origin"]})
        elif bare(str(term["translation"])).casefold() != bare(p["translation"]).casefold():
            out.append({"english": p["english"], "prefilled": p["translation"],
                        "committed": term["translation"], "origin": p["origin"]})
    return out


def overrides_from_answers(
    prefills: dict[str, dict[str, Any]], answers: dict[str, Any], questions: list[dict]
) -> list[dict[str, str]]:
    """House rules the book's answers depart from: ``[{rule_id, question, answer}]``.

    A question a house rule settles, answered with something else, turns that
    rule off for this book (its ``style_rules.json`` records why).
    """
    by_id = {q["id"]: q for q in questions if q.get("id")}
    out = []
    for qid, entry in prefills.items():
        if "rule_id" not in entry or qid not in answers:
            continue
        key, label = normalize_answer(by_id.get(qid), answers[qid])
        if key != entry["house_answer"]:
            out.append({"rule_id": entry["rule_id"], "question": qid, "answer": label})
    return out
