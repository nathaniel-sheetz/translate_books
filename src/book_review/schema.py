"""Validate the synthesis model's recommendations against the book as it is.

A recommendation is only useful if it can be applied, so every patch is checked
here before it reaches the operator:

- a glossary patch names an entry that exists, and any key it introduces
  actually occurs in the source (a fix must not add another dead key);
- a ``before`` text is verbatim in the artifact, exactly once;
- an address patch names a pair the map has;
- a dismissal or follow-up cites findings and chapters that exist;
- every cited id is one the model was shown.

Invalid patches are dropped with the reason recorded. A recommendation with no
valid patch and no follow-up is rejected whole.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from typing import Any, Optional

from src.book_review.corpus import BookCorpus
from src.book_review.patches import apply_address, apply_glossary, apply_style
from src.house.match import term_occurs

PATCH_TYPES = frozenset({
    "glossary.rekey", "glossary.add_variant", "glossary.edit", "glossary.split_sense",
    "glossary.add", "glossary.remove",
    "style.replace_text", "style.append",
    "address.set_direction", "address.replace_global_rules_text",
    "judge.dismiss", "rejudge", "retranslate", "house.rule_proposal", "manual",
})
FOLLOW_UP_TYPES = frozenset({"rejudge", "retranslate"})
CONFIDENCE = ("high", "medium", "low")

#: Which artifact file each patch type edits, for preconditions and conflicts.
ARTIFACT_OF = {
    "glossary": "glossary", "style": "style", "address": "address_map",
}


def artifact_of(patch_type: str) -> Optional[str]:
    return ARTIFACT_OF.get(patch_type.split(".", 1)[0])


def _str(value: Any) -> str:
    return str(value or "").strip()


def _occurs_in_source(corpus: BookCorpus, key: str) -> bool:
    return any(term_occurs(key, c.source_text.lower()) for c in corpus.chunks.values())


def _glossary_key(corpus: BookCorpus, english: str) -> Optional[str]:
    """The entry's key as stored, matched case-insensitively."""
    wanted = english.strip().lower()
    for t in corpus.glossary:
        if str(t.get("english") or "").strip().lower() == wanted:
            return str(t["english"])
    return None


def _pair(corpus: BookCorpus, a: str, b: str) -> Optional[tuple[dict, bool]]:
    """``(pair, swapped)``: the stored pair, and whether ``a``/``b`` arrived reversed."""
    for p in (corpus.address_map or {}).get("pairs") or []:
        if not isinstance(p, dict):
            continue
        if p.get("a") == a and p.get("b") == b:
            return p, False
        if p.get("a") == b and p.get("b") == a:
            return p, True
    return None


def check_patch(patch: Any, corpus: BookCorpus, finding_ids: set[str],
                chapters: set[str]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """``(clean patch, None)`` or ``(None, why it was dropped)``."""
    if not isinstance(patch, dict):
        return None, "not an object"
    kind = _str(patch.get("type"))
    if kind not in PATCH_TYPES:
        return None, f"unknown patch type {kind!r}"
    out: dict[str, Any] = {"type": kind}

    if kind.startswith("glossary."):
        if kind == "glossary.add":
            english, spanish = _str(patch.get("english")), _str(patch.get("spanish"))
            if not english or not spanish:
                return None, "glossary.add needs english and spanish"
            if _glossary_key(corpus, english):
                return None, f"glossary already has {english!r}"
            if not _occurs_in_source(corpus, english):
                return None, f"new key {english!r} never occurs in the source"
            out.update(english=english, spanish=spanish,
                       alternatives=[_str(a) for a in patch.get("alternatives") or [] if _str(a)],
                       context=_str(patch.get("context")) or None)
            return out, None
        key = _glossary_key(corpus, _str(patch.get("english")))
        if not key:
            return None, f"no glossary entry {patch.get('english')!r}"
        out["english"] = key
        if kind in ("glossary.rekey", "glossary.add_variant"):
            new = _str(patch.get("new_english"))
            if not new or new == key:
                return None, f"{kind} needs a different new_english"
            if _glossary_key(corpus, new):
                return None, f"glossary already has {new!r}"
            if not _occurs_in_source(corpus, new):
                return None, f"new key {new!r} never occurs in the source"
            out["new_english"] = new
        elif kind == "glossary.edit":
            changes = {k: patch[k] for k in ("spanish", "alternatives", "context") if k in patch}
            if not changes:
                return None, "glossary.edit changes nothing"
            if "spanish" in changes and not _str(changes["spanish"]):
                return None, "glossary.edit cannot blank spanish"
            if "alternatives" in changes:
                changes["alternatives"] = [_str(a) for a in changes["alternatives"] or [] if _str(a)]
            out.update(changes)
        elif kind == "glossary.split_sense":
            entries = []
            for e in patch.get("entries") or []:
                if not isinstance(e, dict) or not _str(e.get("english")) or not _str(e.get("spanish")):
                    return None, "split_sense entries need english and spanish"
                if not _occurs_in_source(corpus, _str(e["english"])):
                    return None, f"split key {e['english']!r} never occurs in the source"
                entries.append({"english": _str(e["english"]), "spanish": _str(e["spanish"]),
                                "context": _str(e.get("context")) or None})
            if len(entries) < 2:
                return None, "split_sense needs at least two entries"
            out["entries"] = entries
        return out, None

    if kind == "style.replace_text":
        before, after = str(patch.get("before") or ""), str(patch.get("after") or "")
        if not before.strip():
            return None, "style.replace_text needs before"
        count = corpus.style_text.count(before)
        if count != 1:
            return None, f"before text occurs {count} times in the style guide"
        out.update(before=before, after=after)
        return out, None
    if kind == "style.append":
        text = _str(patch.get("text"))
        if not text:
            return None, "style.append needs text"
        out["text"] = text
        return out, None

    if kind == "address.set_direction":
        if not corpus.address_map:
            return None, "the book has no address map"
        found = _pair(corpus, _str(patch.get("a")), _str(patch.get("b")))
        if not found:
            return None, f"no address pair {patch.get('a')!r}/{patch.get('b')!r}"
        pair, swapped = found
        direction = _str(patch.get("direction"))
        if direction not in ("a_to_b", "b_to_a"):
            return None, "direction must be a_to_b or b_to_a"
        if swapped:
            direction = "b_to_a" if direction == "a_to_b" else "a_to_b"
        form = _str(patch.get("form")).lower()
        form = "tú" if form in ("tú", "tu") else "usted" if form == "usted" else ""
        if not form:
            return None, "form must be tú or usted"
        out.update(a=pair["a"], b=pair["b"], direction=direction, form=form,
                   notes=_str(patch.get("notes")) or None)
        return out, None
    if kind == "address.replace_global_rules_text":
        rules = str((corpus.address_map or {}).get("global_rules") or "")
        before = str(patch.get("before") or "")
        if not before.strip() or rules.count(before) != 1:
            return None, "before text must occur exactly once in global_rules"
        out.update(before=before, after=str(patch.get("after") or ""))
        return out, None

    if kind == "judge.dismiss":
        ids = [i for i in patch.get("finding_ids") or [] if isinstance(i, str)]
        unknown = [i for i in ids if i not in finding_ids]
        if not ids or unknown:
            return None, f"unknown or missing finding ids {unknown[:3]}"
        kept = {f.id for f in corpus.findings if f.status == "kept"}
        ids = [i for i in ids if i in kept]
        if not ids:
            return None, "only kept findings can be dismissed"
        feedback = _str(patch.get("feedback_type")) or "false_positive"
        if feedback not in ("false_positive", "missing_context_gap", "bad_message"):
            return None, f"feedback_type {feedback!r} is not a dismissal"
        out.update(finding_ids=ids, feedback_type=feedback)
        return out, None
    if kind in FOLLOW_UP_TYPES:
        wanted = [c for c in patch.get("chapters") or [] if isinstance(c, str)]
        bad = [c for c in wanted if c not in chapters]
        if not wanted or bad:
            return None, f"unknown or missing chapters {bad[:3]}"
        out["chapters"] = wanted
        if _str(patch.get("why")):
            out["why"] = _str(patch["why"])
        return out, None
    if kind == "house.rule_proposal":
        rule = _str(patch.get("rule"))
        if not rule:
            return None, "house.rule_proposal needs rule"
        out.update(rule=rule, why=_str(patch.get("why")) or None)
        return out, None
    description = _str(patch.get("description"))
    if not description:
        return None, "manual needs description"
    out["description"] = description
    return out, None


def recommendation_id(theme: str, title: str) -> str:
    return "R-" + hashlib.sha1(f"{theme}|{title}".encode("utf-8")).hexdigest()[:6]


def check_recommendation(
    raw: Any, theme: str, corpus: BookCorpus, known_ids: set[str],
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """``(clean recommendation or None, problems)``."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return None, ["not an object"]
    title = _str(raw.get("title"))
    if not title:
        return None, ["no title"]
    finding_ids = {f.id for f in corpus.findings}
    chapters = {c.chapter_id for c in corpus.chunks.values()}

    evidence = [e for e in raw.get("evidence") or [] if isinstance(e, str)]
    unknown = [e for e in evidence if e not in known_ids]
    if unknown:
        problems.append(f"dropped unknown evidence ids {unknown[:5]}")
    evidence = [e for e in evidence if e in known_ids]

    # Patches are replayed in order on working copies, so each one is checked
    # against the artifact as the earlier ones leave it: "rekey X to Y, then add
    # Z as a variant of Y" is valid even though Y does not exist yet.
    patches = []
    view = replace(
        corpus,
        glossary=copy.deepcopy(corpus.glossary),
        address_map=copy.deepcopy(corpus.address_map),
    )
    for n, patch in enumerate(raw.get("patches") or []):
        clean, why = check_patch(patch, view, finding_ids, chapters)
        if not clean:
            problems.append(f"patch {n + 1} dropped: {why}")
            continue
        patches.append(clean)
        family = clean["type"].split(".", 1)[0]
        try:
            if family == "glossary":
                doc = {"terms": view.glossary}
                apply_glossary(doc, clean)
                view.glossary = doc["terms"]
            elif family == "style":
                doc = {"content": view.style_text}
                apply_style(doc, clean)
                view.style_text = doc["content"]
            elif family == "address" and view.address_map is not None:
                apply_address(view.address_map, clean)
        except ValueError as exc:
            patches.pop()
            problems.append(f"patch {n + 1} dropped: {exc}")
    follow_ups = []
    for n, item in enumerate(raw.get("follow_ups") or []):
        if isinstance(item, dict) and _str(item.get("type")) in FOLLOW_UP_TYPES:
            clean, why = check_patch(item, corpus, finding_ids, chapters)
            if clean:
                follow_ups.append(clean)
            else:
                problems.append(f"follow-up {n + 1} dropped: {why}")
    if not patches and not follow_ups:
        return None, problems + ["no valid patch or follow-up"]

    confidence = _str(raw.get("confidence")).lower()
    try:
        priority = int(raw.get("priority") or 3)
    except (TypeError, ValueError):
        priority = 3
    return {
        "id": recommendation_id(theme, title),
        "theme": theme,
        "title": title,
        "rationale": _str(raw.get("rationale")),
        "evidence": evidence,
        "confidence": confidence if confidence in CONFIDENCE else "medium",
        "priority": min(3, max(1, priority)),
        "patches": patches,
        "follow_ups": follow_ups,
        "problems": problems,
    }, problems


def _target(patch: dict[str, Any]) -> Optional[str]:
    kind = patch["type"]
    if kind.startswith("glossary."):
        return f"glossary:{patch['english'].lower()}"
    if kind == "style.replace_text":
        return f"style:{patch['before'][:60]}"
    if kind == "address.set_direction":
        return f"address:{patch['a']}|{patch['b']}|{patch['direction']}"
    if kind == "address.replace_global_rules_text":
        return f"address:global:{patch['before'][:60]}"
    return None


def reconcile(recommendations: list[dict[str, Any]]) -> None:
    """Mark recommendations whose patches touch the same target (in place)."""
    owners: dict[str, list[str]] = {}
    for rec in recommendations:
        for patch in rec["patches"]:
            target = _target(patch)
            if target:
                owners.setdefault(target, []).append(rec["id"])
    for rec in recommendations:
        clashes = sorted({
            other for patch in rec["patches"] for other in owners.get(_target(patch) or "", [])
            if other != rec["id"]
        })
        if clashes:
            rec["conflicts_with"] = clashes
