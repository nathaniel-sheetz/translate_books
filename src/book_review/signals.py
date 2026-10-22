"""Coded detectors over one book's judge findings.

Each detector is a pure function of a :class:`~src.book_review.corpus.BookCorpus`
and returns :class:`Signal` objects: a named pattern, the findings that show it,
the artifact it implicates, and a score used only to order the report. None of
them calls a model and none writes anything.

What each one looks for, and the case that motivated it:

- :func:`dead_glossary_keys` (D1) -- glossary entries that never reach the
  translator because their key never matches the source as written:
  ``porte cochere`` against ``porte-cochere``, ``'Lias`` against ``’Lias``,
  ``stalk (mushroom)``, ``the old stag / the old Prince``. The editorial judge
  reads the whole glossary, so it flags the translator for entries it never saw;
  those are the *blind-spot* findings this detector counts.
- :func:`glossary_sense` (D2) -- an entry applied where the source uses the
  word in another sense: the translator never sees the entry's ``context``, so
  a homonym key ("scales → balanza") lands everywhere the word occurs.
- :func:`address_style_conflicts` (D3) -- the style guide (which the translator
  reads) and the address map (which the address judge reads) disagree about a
  pair, or the map disagrees with itself. Shows up as the address judge and the
  editorial judge pushing the same speaker in opposite directions.
- :func:`rule_drift` (D4) -- findings judged against rules the translation
  never saw: a dialogue block, judge prompt, style guide or address map that
  changed after the chunks were translated.
- :func:`recurring_rules` (D5) -- one rule firing across many chapters. Accepted
  every time means the translator keeps missing a rule it was given (a coded
  check or a stronger instruction is the fix); dismissed every time means the
  rule or the judge is wrong.
- :func:`opposite_directions` (D6) -- one word pushed to another in some
  chapters and back again in others.
- :func:`false_positive_clusters` (D7) -- dismissals bunched on one rule:
  a miscalibrated judge or a stale glossary entry.
- :func:`coverage` (D9) -- what has not been judged yet, so the report can say
  what it could not see.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from src.book_review.corpus import BookCorpus, Finding
from src.house.match import entry_occurs, term_variants

#: Marks that mean "the finding was right".
ACCEPT_MARKS = frozenset({"applied", "resolved"})
#: Marks that mean "the finding was wrong, or could not be judged as written".
REJECT_MARKS = frozenset({"false_positive", "missing_context_gap", "bad_message"})

#: Themes so generic that a cluster of them says nothing about the book's setup.
GENERIC_THEMES = frozenset({"calque", "grammar", "naturalness", "agreement", "other"})

RECURRING_MIN_CHAPTERS = 3
RECURRING_MIN_FINDINGS = 5
FP_CLUSTER_MIN = 3


@dataclass
class Signal:
    id: str
    detector: str
    title: str
    score: float
    chapters: list[str] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    #: ``"A"`` (needs full evidence), ``"B"`` or ``"C"`` -- see evidence tiering.
    tier_hint: str = "B"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _chapters(findings: Iterable[Finding]) -> list[str]:
    return sorted({f.chapter_id for f in findings})


# ---------------------------------------------------------------------------
# D1 dead glossary keys

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'"})
_DASHES_RE = re.compile(r"[-‐‑‒–—]+")
_SPACES_RE = re.compile(r"\s+")
_PAREN_RE = re.compile(r"\s*\([^)]*\)")


def _fold(text: str) -> str:
    """Lower-case, straighten apostrophes, read hyphens as spaces, squeeze spaces."""
    text = (text or "").lower().translate(_APOSTROPHES)
    text = _DASHES_RE.sub(" ", text)
    return _SPACES_RE.sub(" ", text).strip()


def _word_re(form: str) -> re.Pattern:
    body = re.escape(form).replace(r"\ ", " ")
    return re.compile(r"(?<![\w'])" + body + r"(?:e?s|'s)?(?![\w])")


def loose_variants(key: str) -> list[tuple[str, str]]:
    """``(folded form, reason)`` pairs a key's English could also appear as.

    Ordered strongest first. ``head_noun`` (the last word of a multi-word key)
    is the weakest -- "dogs" for "prairie dog" -- and the detector only trusts
    it where a finding corroborates it.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(form: str, reason: str) -> None:
        form = _fold(form)
        if len(form) >= 3 and form not in seen:
            seen.add(form)
            out.append((form, reason))

    base = key.strip()
    add(base, "punctuation")
    stripped = _PAREN_RE.sub("", base).strip()
    if stripped != base:
        add(stripped, "parenthetical")
    if "/" in stripped:
        for part in stripped.split("/"):
            add(part, "slash")
    for form, _ in list(out):
        if form.endswith("y") and not form.endswith(("ay", "ey", "oy", "uy")):
            add(form[:-1] + "ies", "plural_ies")
    words = _fold(stripped).split()
    if len(words) > 1 and len(words[-1]) >= 3:
        add(words[-1], "head_noun")
    return out


def _linked(finding: Finding, term: dict[str, Any]) -> bool:
    """Whether a finding is about this glossary entry, not merely near it."""
    text = finding.text().lower()
    if finding.theme != "glossary" and "glosario" not in text and "glossary" not in text:
        return False
    spanish = [term.get("spanish") or ""] + list(term.get("alternatives") or [])
    if any(len(s) >= 3 and s.lower() in text for s in spanish):
        return True
    english = (term.get("english") or "").lower()
    return len(english) >= 4 and english in text


def _spanish_forms(term: dict[str, Any]) -> list[str]:
    forms = [term.get("spanish") or ""] + list(term.get("alternatives") or [])
    return [s.lower() for s in forms if len(s) >= 3]


def dead_glossary_keys(corpus: BookCorpus) -> list[Signal]:
    chunks = corpus.chunks_in_order()
    folded_source = {c.id: _fold(c.source_text) for c in chunks}
    lowered_source = {c.id: c.source_text.lower() for c in chunks}
    findings_by_chunk: dict[str, list[Finding]] = defaultdict(list)
    for f in corpus.findings:
        findings_by_chunk[f.chunk_id].append(f)

    by_key = {str(t.get("english") or "").strip().lower(): t for t in corpus.glossary}

    def shipped_keys(chunk) -> set[str]:
        if chunk.shipped_glossary is not None:
            return chunk.shipped_glossary
        return {k for k, t in by_key.items()
                if entry_occurs(t["english"], t.get("aliases") or [], lowered_source[chunk.id])}

    shipped_by_chunk = {c.id: shipped_keys(c) for c in chunks}

    def covered_by_another(finding: Finding, term: dict[str, Any]) -> bool:
        """A sibling entry with the same Spanish reached this chunk ("the game" for "glad game")."""
        own = set(_spanish_forms(term))
        return any(
            own & set(_spanish_forms(by_key[k]))
            for k in shipped_by_chunk.get(finding.chunk_id, ())
            if k in by_key and k != str(term.get("english") or "").strip().lower()
        )

    # One finding is about one entry: the one whose Spanish (or English) it
    # quotes longest. "el juego de alegrarse" belongs to "the game", not to
    # "being glad → alegrarse" merely because it contains the word.
    best_term: dict[str, str] = {}
    for f in corpus.findings:
        best = (0, None)
        text = f.text().lower()
        for k, t in by_key.items():
            if not _linked(f, t):
                continue
            length = max([len(s) for s in _spanish_forms(t) if s in text] + [len(k) if k in text else 0])
            if length > best[0]:
                best = (length, k)
        if best[1]:
            best_term[f.id] = best[1]

    signals: list[Signal] = []
    absent: list[str] = []
    for term in corpus.glossary:
        key = str(term.get("english") or "").strip()
        if not key:
            continue
        key_lower = key.lower()

        shipped = {c.id for c in chunks if key_lower in shipped_by_chunk[c.id]}

        missed: dict[str, str] = {}
        weak: dict[str, str] = {}
        for c in chunks:
            # The key is in the text as written, yet the logged prompt lacks it:
            # the entry was added (or the filter changed) after translation.
            # Literal on purpose: the looser forms are the reasons below.
            if c.id not in shipped and c.shipped_glossary is not None \
                    and any(v in lowered_source[c.id] for v in term_variants(key_lower)):
                missed[c.id] = "not_in_logged_prompt"
        for form, reason in loose_variants(key):
            pattern = _word_re(form)
            for c in chunks:
                if c.id in shipped or c.id in missed or c.id in weak:
                    continue
                if pattern.search(folded_source[c.id]):
                    (weak if reason == "head_noun" else missed)[c.id] = reason

        linked = [
            f for c in chunks for f in findings_by_chunk.get(c.id, [])
            if best_term.get(f.id) == key_lower
        ]
        # A head-noun hit is a guess ("dogs" may be dogs); keep it only where a
        # finding about this entry fell in that very chunk.
        for f in linked:
            if f.chunk_id in weak and not covered_by_another(f, term):
                missed.setdefault(f.chunk_id, "head_noun")
        # Blind spot: the judge holds the translator to an entry the source
        # needed there and the prompt did not carry (nor any sibling entry).
        blind = [f for f in linked if f.chunk_id in missed and not covered_by_another(f, term)]

        if not shipped and not missed and not linked:
            absent.append(key)
            continue
        if not missed and not blind:
            continue

        reasons = Counter(missed.values())
        if set(reasons) == {"not_in_logged_prompt"}:
            state = "added_after_translation"
            title = (f"Glossary entry '{key}' is missing from the prompts {len(missed)} chunks "
                     f"were translated with (added or changed later?)")
        elif not shipped:
            state = "dead"
            title = f"Glossary entry '{key}' never reached the translator"
        else:
            state = "partial"
            title = (f"Glossary entry '{key}' reached {len(shipped)} of "
                     f"{len(shipped) + len(missed)} chunks that need it")
        signals.append(Signal(
            id=f"D1:{key_lower}",
            detector="dead_glossary_keys",
            title=title,
            score=3.0 * len(blind) + len(missed),
            chapters=sorted({c.split("_chunk")[0] for c in missed} | {f.chapter_id for f in blind}),
            finding_ids=[f.id for f in blind],
            artifact_refs=[f"glossary:{key}"],
            metrics={
                "state": state,
                "spanish": term.get("spanish"),
                "context": term.get("context"),
                "shipped_chunks": len(shipped),
                "missed_chunks": len(missed),
                "reasons": dict(reasons),
                "blind_spot_findings": len(blind),
                "linked_findings": len(linked),
                "logged_prompts": sum(1 for c in chunks if c.shipped_glossary is not None),
            },
            tier_hint="A" if blind else "B",
        ))

    if absent:
        signals.append(Signal(
            id="D1:absent",
            detector="dead_glossary_keys",
            title=f"{len(absent)} glossary entries match nothing in the source",
            score=0.5,
            artifact_refs=[f"glossary:{k}" for k in absent],
            metrics={"state": "absent", "keys": absent},
            tier_hint="C",
        ))
    return sorted(signals, key=lambda s: -s.score)


# ---------------------------------------------------------------------------
# D3 address map vs style guide

_FORM_TU_RE = re.compile(r"\btú\b|\btuteo\b|\btutea", re.IGNORECASE)
_FORM_USTED_RE = re.compile(r"\busted\b", re.IGNORECASE)
_MUTUAL_RE = re.compile(
    r"\bbetween\b|each other|one another|both ways|both directions|\bmutual|"
    r"\b(?:tú|usted)\s+with\b",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(r"[;.()\n]|\s—\s|\bwhile\b")
_ARTICLE_RE = re.compile(r"^(?:el|la|los|las|the)\s+", re.IGNORECASE)

_TU_WORDS = frozenset({"tú", "tu", "tus", "te", "ti", "contigo", "tuyo", "tuya"})
_USTED_WORDS = frozenset({"usted", "ustedes", "su", "sus", "le", "les", "suyo", "suya"})
_PUSH_MESSAGE_RE = re.compile(
    r"(?:exige|pide|requiere|corresponde|debe(?:n)?\s+(?:ser|usar|ir)|expected|requires|"
    r"must\s+use|should\s+use)\s+(?:el\s+|de\s+)?(tú|tuteo|usted)",
    re.IGNORECASE,
)


def _clause_form(clause: str) -> Optional[str]:
    tu = bool(_FORM_TU_RE.search(clause))
    usted = bool(_FORM_USTED_RE.search(clause))
    if tu == usted:
        return None
    return "tú" if tu else "usted"


def _forms(segments: Any) -> set[str]:
    out = set()
    for seg in segments or []:
        form = str((seg or {}).get("form") or "").strip().lower()
        if form.startswith("us"):
            out.add("usted")
        elif form.startswith("t"):
            out.add("tú")
    return out


_TITLE_WORDS = frozenset({
    "aunt", "uncle", "miss", "mr", "mrs", "dr", "old", "sir", "lady", "lord", "dame",
    "doctor", "father", "mother", "brother", "sister", "cousin", "grandpa", "grandma",
    "master", "mistress", "señor", "señora", "señorita", "tía", "tío", "don", "doña",
    "prima", "primo", "abuelo", "abuela", "padre", "madre", "viejo", "vieja", "doctora",
    "sr", "sra", "srta", "el", "la", "los", "las", "the", "de", "del",
})


def _party_names(name: str, glossary: list[dict[str, Any]]) -> set[str]:
    """Every way the book may spell a party: the map's form, bare, and its glossary pair."""
    names = {name.strip()}
    bare = _ARTICLE_RE.sub("", name.strip())
    names.add(bare)
    for term in glossary:
        english = str(term.get("english") or "")
        spanish = str(term.get("spanish") or "")
        if bare and bare.lower() in {_ARTICLE_RE.sub("", spanish).lower(), _ARTICLE_RE.sub("", english).lower()}:
            names.update({english, spanish, _ARTICLE_RE.sub("", english), _ARTICLE_RE.sub("", spanish)})
    return {n for n in names if len(n) >= 3}


def _proper_tokens(names: set[str]) -> set[str]:
    """The given name or surname inside each spelling: "Miss Polly" and "la tía Polly" share "Polly"."""
    out = set()
    for name in names:
        for token in re.findall(r"[\wÁÉÍÓÚÑáéíóúñ'’-]+", name):
            if len(token) >= 3 and token[0].isupper() and token.lower().rstrip(".") not in _TITLE_WORDS:
                out.add(token)
    return out


def _all_party_names(parties: Iterable[str], glossary: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Spellings per party, plus each party's proper-name tokens no other party shares."""
    names = {p: _party_names(p, glossary) for p in parties}
    tokens = {p: _proper_tokens(n) for p, n in names.items()}
    counts = Counter(t for ts in tokens.values() for t in ts)
    for party, ts in tokens.items():
        names[party] |= {t for t in ts if counts[t] == 1}
    return names


def _mentions(text: str, names: set[str]) -> int:
    """Position of the first mention of any name in ``text``, or -1."""
    best = -1
    lowered = text.lower()
    for name in names:
        match = re.search(r"(?<!\w)" + re.escape(name.lower()) + r"(?!\w)", lowered)
        if match and (best < 0 or match.start() < best):
            best = match.start()
    return best


def _pushed_form(finding: Finding) -> Optional[str]:
    """Which form an editorial finding's suggestion moves the excerpt toward."""
    before = re.findall(r"[\wáéíóúñü]+", finding.location.lower())
    after = re.findall(r"[\wáéíóúñü]+", finding.suggestion.lower())
    removed = Counter(before) - Counter(after)
    added = Counter(after) - Counter(before)
    score = 0
    score += sum(n for w, n in added.items() if w in _USTED_WORDS)
    score -= sum(n for w, n in added.items() if w in _TU_WORDS)
    score -= sum(n for w, n in removed.items() if w in _USTED_WORDS)
    score += sum(n for w, n in removed.items() if w in _TU_WORDS)
    # sabes -> sabe, quieres -> quiere: the second-person -s dropped.
    for word in added:
        if word + "s" in removed:
            score += 1
        if word.endswith("s") and word[:-1] in removed:
            score -= 1
    if score:
        return "usted" if score > 0 else "tú"
    match = _PUSH_MESSAGE_RE.search(finding.message)
    if match:
        return "usted" if match.group(1).lower().startswith("usted") else "tú"
    return None


def address_style_conflicts(corpus: BookCorpus) -> list[Signal]:
    amap = corpus.address_map
    if not amap:
        return []
    pairs = [p for p in amap.get("pairs") or [] if isinstance(p, dict) and p.get("a") and p.get("b")]
    if not pairs:
        return []
    names = _all_party_names({p["a"] for p in pairs} | {p["b"] for p in pairs}, corpus.glossary)

    texts = [("style guide", corpus.style_text or "")]
    texts.append(("address map global_rules", str(amap.get("global_rules") or "")))
    signals: list[Signal] = []

    # Textual: a clause that names both parties and states one form.
    for p in pairs:
        a, b = p["a"], p["b"]
        directions = p.get("directions") or {}
        ab, ba = _forms(directions.get("a_to_b")), _forms(directions.get("b_to_a"))
        for source, text in texts:
            for clause in _CLAUSE_SPLIT_RE.split(text):
                pos_a, pos_b = _mentions(clause, names[a]), _mentions(clause, names[b])
                if pos_a < 0 or pos_b < 0:
                    continue
                # "Paul and Maureen use usted with their grandparents": the two
                # names are one coordinated subject, not speaker and addressee.
                between = clause[min(pos_a, pos_b):max(pos_a, pos_b)]
                if re.search(r"^\S+(?:\s+\S+)?\s*(?:,|\band\b|\by\b|&)\s*\S*$", between.strip()):
                    continue
                form = _clause_form(clause)
                if not form:
                    continue
                if _MUTUAL_RE.search(clause):
                    checked = {f"{a}→{b}": ab, f"{b}→{a}": ba}
                elif pos_a < pos_b:
                    checked = {f"{a}→{b}": ab}
                else:
                    checked = {f"{b}→{a}": ba}
                clash = {d: sorted(forms) for d, forms in checked.items() if forms and form not in forms}
                if not clash:
                    continue
                signals.append(Signal(
                    id=f"D3:text:{source}:{a}→{b}:{len(signals)}",
                    detector="address_style_conflicts",
                    title=f"The {source} says {form} for {a} and {b}; the address map says otherwise",
                    score=4.0 if source == "style guide" else 3.0,
                    artifact_refs=[f"style:{clause.strip()[:80]}" if source == "style guide"
                                   else f"address:global_rules", f"address:{a}→{b}"],
                    metrics={"source": source, "clause": clause.strip(), "clause_form": form,
                             "map_forms": clash, "pair": [a, b]},
                    tier_hint="A",
                ))

    # Empirical: the address judge and the editorial judge pull one pair apart.
    def resolve(name: Optional[str]) -> Optional[str]:
        """The map party a judge's free-text speaker name means, by its longest match."""
        if not name:
            return None
        best: tuple[int, Optional[str]] = (0, None)
        lowered = name.lower()
        for party, variants in names.items():
            for v in variants:
                if len(v) > best[0] and re.search(r"(?<!\w)" + re.escape(v.lower()) + r"(?!\w)", lowered):
                    best = (len(v), party)
        return best[1]

    address_expect: dict[tuple[str, str], Counter] = defaultdict(Counter)
    address_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
    for f in corpus.findings:
        if f.judge != "address" or f.status != "kept" or not f.pair or not f.expected_form:
            continue
        speaker, addressee = resolve(f.pair[0]), resolve(f.pair[1])
        if speaker and addressee:
            address_expect[(speaker, addressee)][f.expected_form] += 1
            address_ids[(speaker, addressee)].append(f.id)

    editorial_push: dict[tuple[str, str], Counter] = defaultdict(Counter)
    editorial_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
    for f in corpus.findings:
        if f.judge != "editorial" or f.status != "kept" or f.theme != "address":
            continue
        form = _pushed_form(f)
        if not form:
            continue
        hits = sorted(
            ((pos, party) for party, variants in names.items()
             if (pos := _mentions(f.message, variants)) >= 0),
        )
        if len(hits) < 2:
            continue
        key = (hits[0][1], hits[1][1])
        editorial_push[key][form] += 1
        editorial_ids[key].append(f.id)

    # The address judge against its own map: an explicit, unconditional pair
    # entry says one form and the judge keeps demanding the other (usually a
    # global rule overriding the pair, as with "child to adult stranger").
    fixed_forms: dict[tuple[str, str], set[str]] = {}
    for p in pairs:
        for direction, (s, t) in (("a_to_b", (p["a"], p["b"])), ("b_to_a", (p["b"], p["a"]))):
            segments = (p.get("directions") or {}).get(direction) or []
            conditional = any(
                seg.get("since") or seg.get("until") or seg.get("after_event")
                or (seg.get("when") and seg.get("when") != "default")
                for seg in segments if isinstance(seg, dict)
            )
            if segments and not conditional:
                fixed_forms[(s, t)] = _forms(segments)
    for key, expected in address_expect.items():
        forms = fixed_forms.get(key)
        if not forms:
            continue
        against = {form: n for form, n in expected.items() if form not in forms}
        if not against:
            continue
        speaker, addressee = key
        ids = [
            i for i in address_ids[key]
            if next((f for f in corpus.findings if f.id == i), None).expected_form in against
        ]
        signals.append(Signal(
            id=f"D3:map:{speaker}→{addressee}",
            detector="address_style_conflicts",
            title=(f"{speaker}→{addressee}: the address judge demands "
                   f"{', '.join(against)} ({sum(against.values())}×) but the map's own pair says "
                   f"{', '.join(sorted(forms))}"),
            score=1.0 + sum(against.values()) * 0.5,
            chapters=_chapters(f for f in corpus.findings if f.id in set(ids)),
            finding_ids=ids,
            artifact_refs=[f"address:{speaker}→{addressee}", "address:global_rules"],
            metrics={"pair": [speaker, addressee], "map_forms": sorted(forms),
                     "address_expected": dict(expected)},
            tier_hint="A",
        ))

    for key, expected in address_expect.items():
        pushed = editorial_push.get(key)
        if not pushed:
            continue
        a_form, a_n = expected.most_common(1)[0]
        e_form, e_n = pushed.most_common(1)[0]
        if a_form == e_form:
            continue
        speaker, addressee = key
        # Interleaved, so evidence capped at the first dozen shows both sides.
        a_ids, e_ids = address_ids[key], editorial_ids[key]
        all_ids = [i for pair in zip(a_ids, e_ids) for i in pair]
        all_ids += a_ids[len(e_ids):] + e_ids[len(a_ids):]
        signals.append(Signal(
            id=f"D3:judges:{speaker}→{addressee}",
            detector="address_style_conflicts",
            title=(f"{speaker}→{addressee}: the address judge wants {a_form} ({a_n}×), "
                   f"the editorial judge wants {e_form} ({e_n}×)"),
            score=2.0 * min(a_n, e_n) + 1.0,
            chapters=_chapters(f for f in corpus.findings if f.id in set(all_ids)),
            finding_ids=all_ids,
            artifact_refs=[f"address:{speaker}→{addressee}", "style:FORMS OF ADDRESS"],
            metrics={"address_expected": dict(expected), "editorial_pushed": dict(pushed),
                     "pair": [speaker, addressee]},
            tier_hint="A",
        ))
    return sorted(signals, key=lambda s: -s.score)


# ---------------------------------------------------------------------------
# D5 recurring rules

def _mark_rates(findings: list[Finding]) -> dict[str, Any]:
    marks = Counter(f.mark for f in findings if f.mark)
    marked = sum(marks.values())
    accepted = sum(n for m, n in marks.items() if m in ACCEPT_MARKS)
    rejected = sum(n for m, n in marks.items() if m in REJECT_MARKS)
    return {
        "marks": dict(marks),
        "marked": marked,
        "unmarked": len(findings) - marked,
        "accept_rate": round(accepted / marked, 2) if marked else None,
        "reject_rate": round(rejected / marked, 2) if marked else None,
    }


def _label(findings: list[Finding], rates: dict[str, Any]) -> str:
    if rates["marked"] < 3:
        return "unreviewed"
    if (rates["reject_rate"] or 0) >= 0.5:
        return "disputed"
    rule_given = any(
        (f.category or "").upper() in {"STYLE_GUIDE", "CONSISTENCY"} or f.theme == "glossary"
        for f in findings
    )
    if rule_given and (rates["accept_rate"] or 0) >= 0.6:
        return "resisted"
    return "recurring"


def recurring_rules(corpus: BookCorpus) -> list[Signal]:
    kept = [f for f in corpus.findings if f.status == "kept"]
    groups: dict[tuple[str, str, str], list[Finding]] = defaultdict(list)
    for f in kept:
        groups[(f.judge, "rule", f.rule.lower())].append(f)
        if f.theme not in GENERIC_THEMES:
            groups[(f.judge, "theme", f.theme)].append(f)

    signals: list[Signal] = []
    rule_sets = {k: {f.id for f in v} for k, v in groups.items() if k[1] == "rule"}
    for (judge, level, name), findings in groups.items():
        chapters = _chapters(findings)
        if len(chapters) < RECURRING_MIN_CHAPTERS and len(findings) < RECURRING_MIN_FINDINGS:
            continue
        if level == "theme":
            ids = {f.id for f in findings}
            # A theme cluster that is really one rule adds nothing.
            if any(len(ids & s) >= 0.8 * len(ids) for k, s in rule_sets.items() if k[0] == judge):
                continue
        rates = _mark_rates(findings)
        label = _label(findings, rates)
        weight = {"resisted": 3.0, "disputed": 2.5, "recurring": 1.0, "unreviewed": 1.0}[label]
        signals.append(Signal(
            id=f"D5:{judge}:{level}:{name}",
            detector="recurring_rules",
            title=f"{judge} {level} '{name}': {len(findings)} findings in {len(chapters)} chapters ({label})",
            score=weight * len(chapters) + 0.1 * len(findings),
            chapters=chapters,
            finding_ids=[f.id for f in findings],
            metrics={"judge": judge, "level": level, "name": name, "label": label,
                     "count": len(findings), "categories": dict(Counter(f.category for f in findings if f.category)),
                     **rates},
            tier_hint="A" if label in {"resisted", "disputed"} else "B",
        ))
    return sorted(signals, key=lambda s: -s.score)


# ---------------------------------------------------------------------------
# D7 false-positive clusters

def false_positive_clusters(corpus: BookCorpus) -> list[Signal]:
    rejected = [f for f in corpus.findings if f.mark in REJECT_MARKS]
    groups: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for f in rejected:
        groups[(f.judge, f.theme)].append(f)
    signals: list[Signal] = []
    for (judge, theme), findings in groups.items():
        if len(findings) < FP_CLUSTER_MIN:
            continue
        marks = Counter(f.mark for f in findings)
        if theme == "glossary":
            label = "glossary_stale"
        elif marks.get("missing_context_gap", 0) * 2 >= len(findings):
            label = "missing_context"
        else:
            label = "judge_miscalibrated"
        rules = Counter(f.rule for f in findings)
        signals.append(Signal(
            id=f"D7:{judge}:{theme}",
            detector="false_positive_clusters",
            title=f"{len(findings)} {judge} findings on '{theme}' were dismissed ({label})",
            score=1.5 * len(findings),
            chapters=_chapters(findings),
            finding_ids=[f.id for f in findings],
            metrics={"judge": judge, "theme": theme, "label": label, "marks": dict(marks),
                     "rules": dict(rules.most_common(8))},
            tier_hint="A",
        ))
    return sorted(signals, key=lambda s: -s.score)


# ---------------------------------------------------------------------------
# D9 coverage

def coverage(corpus: BookCorpus) -> dict[str, Any]:
    """What has not been judged, so the report can say what it could not see."""
    from src.judges.status import build_status

    judges = ["dialogue", "editorial"]
    if corpus.address_map:
        judges.insert(1, "address")
    try:
        status = build_status(corpus.project_dir, judges=judges)
    except Exception as exc:  # a status failure must not sink the review
        return {"error": str(exc)}
    out: dict[str, Any] = {"address_map": bool(corpus.address_map), "judges": {}}
    for name in judges:
        entry = (status.get("judges") or {}).get(name) or {}
        buckets = entry.get("chapters") or {}
        out["judges"][name] = {
            "state": entry.get("state"),
            "owed": [c for state in ("stale", "partial", "not_run") for c in buckets.get(state, [])],
            "done": len(buckets.get("done", [])),
            "last_run": entry.get("last_run"),
        }
    out["unverified_editorial_chunks"] = _unverified_editorial(corpus)
    return out


def _unverified_editorial(corpus: BookCorpus) -> int:
    import json

    count = 0
    for path in (corpus.project_dir / "evaluations").glob("chapter_*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta = (((doc.get("judges") or {}).get("editorial") or {}).get("metadata")) or {}
        if meta and meta.get("candidates") and not meta.get("verified"):
            count += 1
    return count


# ---------------------------------------------------------------------------
# D2 glossary entries applied in the wrong sense

_SENSE_NOTE_RE = re.compile(
    r"\bnot\b|—\s*not|\bonly\b|\bnever\b|\bas in\b|\bwhen\b|\bsense\b|\bno es\b|\bno el\b",
    re.IGNORECASE,
)
CONCORDANCE = 12


def _concordance(corpus: BookCorpus, key: str) -> list[str]:
    out = []
    pattern = re.compile(r"(?<![\w'])" + re.escape(key) + r"(?:e?s)?(?![\w])", re.IGNORECASE)
    for c in corpus.chunks_in_order():
        for m in pattern.finditer(c.source_text):
            start, end = max(0, m.start() - 60), min(len(c.source_text), m.end() + 60)
            out.append(f"{c.chapter_id}: …{c.source_text[start:end].strip()}…".replace("\n", " "))
            if len(out) >= CONCORDANCE:
                return out
    return out


def glossary_sense(corpus: BookCorpus) -> list[Signal]:
    """Entries a judge wants replaced where the source uses the word another way.

    The translator gets ``english → spanish`` and never the ``context`` that
    restricts the sense, so a homonym key ("scales → balanza", "weighing
    instrument, not escamas") is applied wherever the word occurs. The sign is
    a finding, in a chunk the entry reached, whose suggestion removes the
    entry's Spanish. The English concordance lets the reader see the senses.
    """
    by_chunk: dict[str, list[Finding]] = defaultdict(list)
    for f in corpus.findings:
        if f.status == "kept" and f.location and f.suggestion:
            by_chunk[f.chunk_id].append(f)
    signals = []
    for term in corpus.glossary:
        key = str(term.get("english") or "").strip()
        forms = [s for s in _spanish_forms(term) if len(s) >= 4]
        if not key or not forms or key.lower() == str(term.get("spanish") or "").lower():
            continue
        hits = []
        for c in corpus.chunks_in_order():
            reached = (c.shipped_glossary is not None and key.lower() in c.shipped_glossary) or (
                c.shipped_glossary is None
                and entry_occurs(key, term.get("aliases") or [], c.source_text.lower()))
            if not reached:
                continue
            for f in by_chunk.get(c.id, []):
                before, after = f.location.lower(), f.suggestion.lower()
                if any(s in before and s not in after for s in forms):
                    hits.append(f)
        if not hits:
            continue
        note = str(term.get("context") or "")
        signals.append(Signal(
            id=f"D2:{key.lower()}",
            detector="glossary_sense",
            title=f"Judges replace the glossary rendering of '{key}' ({term.get('spanish')}) "
                  f"{len(hits)}× in chunks it reached",
            score=2.0 * len(hits) + (1.0 if _SENSE_NOTE_RE.search(note) else 0.0),
            chapters=_chapters(hits),
            finding_ids=[f.id for f in hits],
            artifact_refs=[f"glossary:{key}"],
            metrics={"spanish": term.get("spanish"), "context": note or None,
                     "sense_note": bool(_SENSE_NOTE_RE.search(note)),
                     "concordance": _concordance(corpus, key),
                     "marks": dict(Counter(f.mark for f in hits if f.mark))},
            tier_hint="A",
        ))
    return sorted(signals, key=lambda s: -s.score)


# ---------------------------------------------------------------------------
# D4 rules written after the translation

def _current_dialogue_rules() -> Optional[str]:
    try:
        from src.book_review.corpus import dialogue_fingerprint
        from src.utils.text_utils import _load_dialogue_block

        return dialogue_fingerprint(_load_dialogue_block())
    except Exception:  # an unreadable rules file just disables this check
        return None


def rule_drift(corpus: BookCorpus, *, current_dialogue: Optional[str] = None) -> list[Signal]:
    """Findings judged against rules the translation never saw.

    Three checks:
    - the DIALOGUE FORMATTING block each chunk's logged prompt carried, against
      the rules the dialogue judge applies now;
    - a judge's own prompt changing partway through the book;
    - the style guide or address map changing after chunks were translated.
    """
    signals: list[Signal] = []
    current = current_dialogue if current_dialogue is not None else _current_dialogue_rules()
    dialogue_by_chunk: dict[str, list[Finding]] = defaultdict(list)
    for f in corpus.findings:
        if f.judge == "dialogue" and f.status == "kept":
            dialogue_by_chunk[f.chunk_id].append(f)

    if current and dialogue_by_chunk:
        groups: dict[str, list] = defaultdict(list)
        for c in corpus.chunks_in_order():
            if c.dialogue_rules is not None and c.dialogue_rules != current:
                groups["none" if c.dialogue_rules == "" else c.dialogue_rules].append(c)
        for fingerprint, chunks in groups.items():
            findings = [f for c in chunks for f in dialogue_by_chunk.get(c.id, [])]
            if not findings:
                continue
            stamps = sorted(c.translated_at for c in chunks if c.translated_at)
            label = ("with no dialogue rules in the prompt" if fingerprint == "none"
                     else "under an older set of dialogue rules")
            signals.append(Signal(
                id=f"D4:dialogue:{fingerprint}",
                detector="rule_drift",
                title=(f"{len(chunks)} chunks were translated {label}; {len(findings)} dialogue "
                       f"findings on them judge against today's rules"),
                score=2.0 + 0.05 * len(findings),
                chapters=_chapters(findings),
                finding_ids=[f.id for f in findings],
                metrics={"kind": "dialogue_rules", "fingerprint": fingerprint, "current": current,
                         "chunks": len(chunks),
                         "translated": [stamps[0][:10], stamps[-1][:10]] if stamps else None,
                         "rules": dict(Counter(f.rule for f in findings).most_common(6)),
                         **_mark_rates(findings)},
                tier_hint="B",
            ))

    by_judge: dict[str, dict[str, list[Finding]]] = defaultdict(lambda: defaultdict(list))
    for f in corpus.findings:
        if f.status == "kept" and f.prompt_version:
            by_judge[f.judge][f.prompt_version].append(f)
    for judge, versions in by_judge.items():
        if len(versions) < 2:
            continue
        rows = {v[:8]: {"findings": len(fs), "chapters": len({f.chapter_id for f in fs}),
                        "judged": sorted(f.executed_at or "" for f in fs)[-1][:10]}
                for v, fs in versions.items()}
        signals.append(Signal(
            id=f"D4:judge-prompt:{judge}",
            detector="rule_drift",
            title=f"The {judge} judge's prompt changed partway through the book ({len(versions)} versions)",
            score=1.5,
            chapters=sorted({f.chapter_id for fs in versions.values() for f in fs}),
            metrics={"kind": "judge_prompt", "judge": judge, "versions": rows},
            tier_hint="C",
        ))

    stamps = [c.translated_at for c in corpus.chunks.values() if c.translated_at]
    if stamps:
        for artifact, doc, field_name in (
            ("style guide", corpus.style_meta, "updated_at"),
            # TODO: created_at means "written", so a map edited after translation
            # (apply stamps updated_at) raises no drift signal. updated_at would
            # also flag every later save, including ones that did not change a
            # rule the translator saw.
            ("address map", corpus.address_map or {}, "created_at"),
        ):
            when = str((doc or {}).get(field_name) or "")[:19]
            if not when:
                continue
            before = [s for s in stamps if s[:19] < when]
            if not before:
                continue
            judge = "address" if artifact == "address map" else "editorial"
            findings = [f for f in corpus.findings if f.judge == judge and f.status == "kept"
                        and (corpus.chunks.get(f.chunk_id) and (corpus.chunks[f.chunk_id].translated_at or "")[:19] < when)]
            if not findings:
                continue
            verb = "written" if field_name == "created_at" else "last changed"
            signals.append(Signal(
                id=f"D4:artifact:{artifact}",
                detector="rule_drift",
                title=(f"The {artifact} was {verb} {when[:10]}, after {len(before)} of "
                       f"{len(stamps)} chunks were translated; {len(findings)} {judge} findings "
                       f"fall on those chunks"),
                score=1.0 + 0.05 * len(findings),
                chapters=_chapters(findings),
                finding_ids=[f.id for f in findings][:200],
                artifact_refs=["address:global_rules"] if artifact == "address map" else [],
                metrics={"kind": "artifact_after_translation", "artifact": artifact, "at": when,
                         "chunks_before": len(before), "chunks": len(stamps),
                         "style_guide_summary": bool((corpus.address_map or {}).get("style_guide_summary"))
                         if artifact == "address map" else None},
                tier_hint="B",
            ))
    return sorted(signals, key=lambda s: -s.score)


# ---------------------------------------------------------------------------
# D6 opposite-direction suggestions

def _swap(f: Finding) -> Optional[tuple[tuple[str, ...], tuple[str, ...]]]:
    before = Counter(re.findall(r"[\wáéíóúñü]+", f.location.lower()))
    after = Counter(re.findall(r"[\wáéíóúñü]+", f.suggestion.lower()))
    removed, added = before - after, after - before
    if 1 <= sum(removed.values()) <= 2 and 1 <= sum(added.values()) <= 2:
        return tuple(sorted(removed)), tuple(sorted(added))
    return None


def opposite_directions(corpus: BookCorpus) -> list[Signal]:
    """One word pushed to another in some chapters and back again in others."""
    swaps: dict[tuple, list[Finding]] = defaultdict(list)
    for f in corpus.findings:
        if f.status == "kept" and f.location and f.suggestion:
            key = _swap(f)
            if key:
                swaps[key].append(f)
    signals = []
    for (removed, added), forward in swaps.items():
        backward = swaps.get((added, removed))
        if not backward or removed > added:
            continue
        findings = forward + backward
        signals.append(Signal(
            id=f"D6:{'+'.join(removed)}~{'+'.join(added)}",
            detector="opposite_directions",
            title=(f"«{' '.join(removed)}» → «{' '.join(added)}» {len(forward)}×, and back "
                   f"{len(backward)}×"),
            score=1.5 * min(len(forward), len(backward)) + 1.0,
            chapters=_chapters(findings),
            finding_ids=[f.id for f in findings],
            metrics={"forward": len(forward), "backward": len(backward),
                     "judges": dict(Counter(f.judge for f in findings)),
                     "rules": dict(Counter(f.rule for f in findings).most_common(6))},
            tier_hint="A",
        ))
    return sorted(signals, key=lambda s: -s.score)


DETECTORS = (
    dead_glossary_keys,
    glossary_sense,
    address_style_conflicts,
    rule_drift,
    recurring_rules,
    false_positive_clusters,
    opposite_directions,
)


def run_all(corpus: BookCorpus) -> dict[str, Any]:
    """Every detector over one book, plus coverage. JSON-ready."""
    signals: list[Signal] = []
    for detector in DETECTORS:
        signals.extend(detector(corpus))
    return {
        "project": corpus.slug,
        "findings": len(corpus.findings),
        "findings_by_judge": dict(Counter(f.judge for f in corpus.findings)),
        "coverage": coverage(corpus),
        "signals": [s.to_dict() for s in signals],
    }
