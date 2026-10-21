"""Turn detector signals into the evidence a synthesis model reads.

The model is asked to say what upstream change would remove a pattern, so it
needs more than the signal's title. It needs the findings themselves, what the
English said, the exact glossary entry, style-guide line or address-map pair
involved, and when each was written relative to the translation. It needs that
in proportion: one dead glossary key with three blind-spot findings deserves its
full record, and the hundredth calque deserves a count.

Three tiers:

- **A (full):** every finding in full (capped at :data:`A_FINDINGS`) with the
  English paragraph it translates, plus the artifacts the signal names.
- **B (compact):** three full exemplars, the rest as id + message.
- **C (count):** counts and two one-line samples.

A detector suggests a tier (``Signal.tier_hint``). The budget decides: jobs are
capped at :data:`JOB_TOKEN_CAP` and the book at :data:`BOOK_TOKEN_CAP`, and when
either is hit the lowest-scoring items drop a tier, each demotion recorded in
``omitted`` so the report can say what the model did not see in full.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any, Optional

from src.book_review.corpus import BookCorpus, ChunkInfo, Finding

A_FINDINGS = 12
B_EXEMPLARS = 3
C_SAMPLES = 2
EXCERPT_CHARS = 400
ENGLISH_CHARS = 700
JOB_TOKEN_CAP = 40_000
#: Below this an item costs too little to be worth shrinking.
MIN_DEMOTE_TOKENS = 1_000
#: Which detectors' signals lose detail first when a budget is hit. Scores are
#: not comparable across detectors, so the order is by kind: generic recurring
#: clusters and surface echoes go before the conflicts and dead keys they echo.
DEMOTE_ORDER = {
    "recurring_rules": 0,
    "opposite_directions": 1,
    "rule_drift": 2,
    "false_positive_clusters": 3,
    "glossary_sense": 4,
    "dead_glossary_keys": 5,
    "address_style_conflicts": 6,
}
BOOK_TOKEN_CAP = 200_000

THEMES = ("glossary", "address_style", "drift", "rules", "dismissals")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def english_for(chunk: Optional[ChunkInfo], excerpt: str) -> Optional[str]:
    """The English paragraph a Spanish excerpt translates, by paragraph position.

    The translation prompt preserves paragraph breaks, so the paragraph holding
    the excerpt has the same index in the source. When the counts differ, the
    position is scaled. ``None`` when the excerpt is not in the chunk.
    """
    if chunk is None or not excerpt:
        return None
    es = _paragraphs(chunk.translated_text)
    en = _paragraphs(chunk.source_text)
    if not es or not en:
        return None
    probe = re.sub(r"\s+", " ", excerpt.strip())[:60]
    index = next(
        (i for i, p in enumerate(es) if probe and probe in re.sub(r"\s+", " ", p)), None
    )
    if index is None:
        return None
    if len(es) != len(en):
        index = min(len(en) - 1, round(index * len(en) / len(es)))
    return _clip(en[index], ENGLISH_CHARS)


def _finding_full(f: Finding, corpus: BookCorpus) -> dict[str, Any]:
    chunk = corpus.chunks.get(f.chunk_id)
    out = {
        "id": f.id,
        "chapter": f.chapter_id,
        "judge": f.judge,
        "status": f.status,
        "rule": f.rule,
        "category": f.category,
        "mark": f.mark,
        "message": f.message,
        "excerpt": _clip(f.location, EXCERPT_CHARS),
        "suggestion": _clip(f.suggestion, EXCERPT_CHARS),
        "english": english_for(chunk, f.location),
        "judged_at": f.executed_at,
        "translated_at": chunk.translated_at if chunk else None,
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def _finding_line(f: Finding) -> dict[str, Any]:
    return {"id": f.id, "chapter": f.chapter_id, "mark": f.mark, "message": _clip(f.message, 160)}


def _style_lines(style: str, needle: str, around: int = 2) -> Optional[str]:
    lines = style.splitlines()
    needle = needle.strip().lower()[:60]
    for i, line in enumerate(lines):
        if needle and needle in line.lower():
            return "\n".join(lines[max(0, i - around): i + around + 1])
    return None


def _artifacts(signal: dict[str, Any], corpus: BookCorpus) -> dict[str, Any]:
    """The exact glossary entries, style lines and map pairs a signal names."""
    out: dict[str, Any] = {}
    glossary = {str(t.get("english") or "").lower(): t for t in corpus.glossary}
    pairs = (corpus.address_map or {}).get("pairs") or []
    for ref in signal.get("artifact_refs") or []:
        kind, _, name = ref.partition(":")
        if kind == "glossary" and name.lower() in glossary:
            out.setdefault("glossary", []).append(glossary[name.lower()])
        elif kind == "style":
            lines = _style_lines(corpus.style_text, name)
            out.setdefault("style_guide_lines", []).append(lines or name)
        elif kind == "address" and name == "global_rules":
            out["address_global_rules"] = (corpus.address_map or {}).get("global_rules")
        elif kind == "address" and "→" in name:
            a, b = name.split("→", 1)
            for p in pairs:
                if {p.get("a"), p.get("b")} == {a, b}:
                    out.setdefault("address_pairs", []).append(p)
    if corpus.history and out:
        touched = {"glossary": "glossary", "style_guide_lines": "style", "address_pairs": "address_map",
                   "address_global_rules": "address_map"}
        wanted = {touched[k] for k in out}
        rows = [
            {k: r.get(k) for k in ("ts", "artifact", "writer", "reason")}
            for r in corpus.history if any(w in str(r.get("artifact") or "") for w in wanted)
        ]
        if rows:
            out["history"] = rows[-6:]
    return out


def _theme_of_signal(signal: dict[str, Any]) -> str:
    """Which synthesis job reads a signal.

    Everything about forms of address goes to one job, whichever detector raised
    it: three jobs each seeing a slice of the same conflict each propose their own
    rewrite of FORMS OF ADDRESS.
    """
    from src.book_review.corpus import theme_of

    detector = signal["detector"]
    metrics = signal.get("metrics") or {}
    if detector in ("dead_glossary_keys", "glossary_sense"):
        return "glossary"
    if detector == "address_style_conflicts":
        return "address_style"
    if detector == "rule_drift":
        if metrics.get("artifact") == "address map" or metrics.get("judge") == "address":
            return "address_style"
        return "drift"
    if detector == "opposite_directions":
        return "address_style" if (metrics.get("judges") or {}).get("address") else "rules"
    if detector == "false_positive_clusters":
        if metrics.get("label") == "glossary_stale":
            return "glossary"
        return "address_style" if metrics.get("theme") == "address" else "dismissals"
    name = str(metrics.get("name") or "")
    family = name if metrics.get("level") == "theme" else theme_of(name, None, "")
    if family == "glossary":
        return "glossary"
    if family == "address" or metrics.get("judge") == "address":
        return "address_style"
    return "rules"


def _item(signal: dict[str, Any], tier: str, corpus: BookCorpus, by_id: dict[str, Finding]) -> dict[str, Any]:
    findings = [by_id[i] for i in signal.get("finding_ids") or [] if i in by_id]
    item: dict[str, Any] = {
        "signal_id": signal["id"],
        "detector": signal["detector"],
        "title": signal["title"],
        "tier": tier,
        "score": signal["score"],
        "chapters": signal.get("chapters") or [],
        "metrics": signal.get("metrics") or {},
        "finding_count": len(findings),
    }
    if tier == "A":
        item["findings"] = [_finding_full(f, corpus) for f in findings[:A_FINDINGS]]
        item["more_findings"] = [_finding_line(f) for f in findings[A_FINDINGS:]]
        item["artifacts"] = _artifacts(signal, corpus)
    elif tier == "B":
        item["findings"] = [_finding_full(f, corpus) for f in findings[:B_EXEMPLARS]]
        item["more_findings"] = [_finding_line(f) for f in findings[B_EXEMPLARS:]]
        item["artifacts"] = _artifacts(signal, corpus)
    else:
        item["samples"] = [_finding_line(f) for f in findings[:C_SAMPLES]]
    if not item.get("more_findings"):
        item.pop("more_findings", None)
    return item


def _size(item: dict[str, Any]) -> int:
    return estimate_tokens(json.dumps(item, ensure_ascii=False))


def build(corpus: BookCorpus, signals_result: dict[str, Any], *,
          job_cap: int = JOB_TOKEN_CAP, book_cap: int = BOOK_TOKEN_CAP) -> dict[str, Any]:
    """Group signals by theme into job-sized, budgeted evidence."""
    by_id = {f.id: f for f in corpus.findings}
    signals = sorted(signals_result.get("signals") or [], key=lambda s: -s["score"])
    tiers = {s["id"]: s.get("tier_hint") or "B" for s in signals}
    omitted: list[dict[str, Any]] = []

    def items_for(theme_signals):
        return [_item(s, tiers[s["id"]], corpus, by_id) for s in theme_signals]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in signals:
        grouped[_theme_of_signal(s)].append(s)

    def pick_victim(pool):
        """The signal to show in less detail next: least valuable, and big enough to matter."""
        candidates = [s for s in pool if tiers[s["id"]] != "C"]
        sized = [(s, _size(_item(s, tiers[s["id"]], corpus, by_id))) for s in candidates]
        worth = [(s, n) for s, n in sized if n >= MIN_DEMOTE_TOKENS] or sized
        if not worth:
            return None
        return min(worth, key=lambda sn: (DEMOTE_ORDER.get(sn[0]["detector"], 0),
                                          sn[0]["score"], -sn[1]))[0]

    def demote_until(theme_signals, cap):
        """Show signals in less detail, a tier at a time, until the set fits ``cap``."""
        items = items_for(theme_signals)
        while sum(_size(i) for i in items) > cap:
            victim = pick_victim(theme_signals)
            if victim is None:
                break
            old = tiers[victim["id"]]
            tiers[victim["id"]] = "B" if old == "A" else "C"
            omitted.append({"signal_id": victim["id"], "from": old, "to": tiers[victim["id"]],
                            "reason": "budget"})
            items = items_for(theme_signals)
        return items

    themes: list[dict[str, Any]] = []
    for theme in THEMES:
        theme_signals = grouped.get(theme) or []
        if not theme_signals:
            continue
        # One job per theme, never split: two jobs each holding half of one
        # conflict each propose their own fix for it. Over the cap, the
        # lowest-scoring signals are shown in less detail instead.
        items = demote_until(theme_signals, job_cap)
        # Full-evidence items first: they are the ones the model must not skim.
        items.sort(key=lambda i: (i["tier"], -i["score"]))
        themes.append({"theme": theme, "jobs": [{"job_id": f"{theme}-1", "items": items}]})

    total = sum(_size(i) for t in themes for j in t["jobs"] for i in j["items"])
    while total > book_cap:
        # Book-wide overflow: demote across themes, least valuable first.
        victim = pick_victim(signals)
        if victim is None:
            break
        old = tiers[victim["id"]]
        tiers[victim["id"]] = "B" if old == "A" else "C"
        omitted.append({"signal_id": victim["id"], "from": old, "to": tiers[victim["id"]],
                        "reason": "book budget"})
        for t in themes:
            for j in t["jobs"]:
                j["items"] = [
                    _item(next(s for s in signals if s["id"] == i["signal_id"]),
                          tiers[i["signal_id"]], corpus, by_id)
                    for i in j["items"]
                ]
        total = sum(_size(i) for t in themes for j in t["jobs"] for i in j["items"])

    in_signals = {i for s in signals for i in s.get("finding_ids") or []}
    rest = [f for f in corpus.findings if f.id not in in_signals and f.status == "kept"]
    background = Counter((f.judge, f.theme) for f in rest)
    return {
        "schema_version": 1,
        "project": corpus.slug,
        "inputs_hash": inputs_hash(corpus),
        "coverage": signals_result.get("coverage"),
        "themes": themes,
        "background": [
            {"judge": j, "theme": t, "count": n} for (j, t), n in background.most_common(25)
        ],
        "omitted": omitted,
        "estimated_tokens": total,
    }


def _file_hash(path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def artifact_hashes(project_dir) -> dict[str, Optional[str]]:
    """Content hashes of the three artifacts a recommendation may patch."""
    return {
        "glossary": _file_hash(project_dir / "glossary.json"),
        "style": _file_hash(project_dir / "style.json"),
        "address_map": _file_hash(project_dir / "address_map.json"),
    }


def inputs_hash(corpus: BookCorpus) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(artifact_hashes(corpus.project_dir), sort_keys=True).encode())
    for f in sorted(corpus.findings, key=lambda f: f.id):
        digest.update(f"{f.id}|{f.mark}|{f.message}".encode("utf-8"))
    return digest.hexdigest()[:16]


def preamble_context(corpus: BookCorpus) -> str:
    """Book-level text every job shares: the style guide and the address map."""
    amap = corpus.address_map or {}
    pairs = []
    for p in amap.get("pairs") or []:
        if not isinstance(p, dict):
            continue
        d = p.get("directions") or {}

        def forms(k):
            return "/".join(
                str(s.get("form")) + (f" since {s['since']}" if s.get("since") else "")
                + (f" until {s['until']}" if s.get("until") else "")
                for s in d.get(k) or [] if isinstance(s, dict)
            )
        pairs.append(f"- {p.get('a')}→{p.get('b')}: {forms('a_to_b')} | "
                     f"{p.get('b')}→{p.get('a')}: {forms('b_to_a')}")
    parts = [
        f"BOOK: {corpus.slug}",
        "",
        "STYLE GUIDE (style.json; the translator reads this):",
        corpus.style_text or "(none)",
    ]
    if amap:
        parts += [
            "",
            "ADDRESS MAP (address_map.json; only the address judge reads this):",
            str(amap.get("content") or "")[:4000],
            "",
            "Global rules: " + str(amap.get("global_rules") or ""),
            "Style-guide summary field present: " + ("yes" if amap.get("style_guide_summary") else "no"),
            "Pairs:",
            *pairs,
        ]
    else:
        parts += ["", "ADDRESS MAP: none"]
    parts += ["", f"GLOSSARY: {len(corpus.glossary)} entries (the relevant ones are quoted in each item)."]
    return "\n".join(parts)
