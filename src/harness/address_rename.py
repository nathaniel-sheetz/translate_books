"""Reconcile an address map's cast with the approved glossary.

The address map is drafted at Step 0B, *before* the glossary exists, so its cast
carries the **English source names** (``flow._format_characters_block`` tells the
drafter not to guess target-language forms — an invented name the glossary later
contradicts is worse than an honest English one). Step 2's ``glossary commit``
then fixes the target-language names and
:func:`src.harness_guard.address_map_name_warnings` names every drifted pair.

Detection was complete; *execution* was not. Applying the rename by hand is an
ordered-substitution problem over nine text fields where the ordering is
load-bearing and silently corruptible — substitute ``Bambi`` before
``Bambi's mother`` and you get ``la madre de Bambi's mother``. This module makes
it mechanical.

    address_map.json (English cast)          glossary.json (approved names)
    ───────────────────────────────          ──────────────────────────────
                     │                                    │
                     └──────────► rename_map() ◄──────────┘ rename_rules()
                                       │  one classify_occurrences() pass
                                       │  over ONE longest-first alternation
                                       ▼
                          .harness/address_map_draft.json ──► address-map commit

**The single-pass alternation is the correctness property.** Because a match is
consumed and never re-scanned, a replacement can never be re-matched by a shorter
rule: ``Bambi's mother`` → ``la madre de Bambi`` is final. Sequential
``str.replace`` calls do not have this property and must not be used here.

That protects one pass from itself; it does not protect the *next* pass. An
approved form may contain an English source name as whole words — its own
(``Detective Smuff`` → ``el detective Smuff``, 3 of the 20 glossaries on disk;
``Signora Patti`` → ``la Signora Patti``) or **another rule's** (``Aunt Harriet``
→ ``la tía Harriet`` standing beside ``Harriet`` → ``Enriqueta``). Either way the
finished text matches on the next run: a second rename yielded
``el el detective Smuff``, and turned ``la tía Harriet`` into ``la tía Enriqueta``
while the stale-cast warning fell silent, so "run it again to confirm" *created*
the drift it was meant to disprove. Every match is therefore first tested against
the approved forms already present in the text, and idempotence holds for all
rules rather than only the self-nested ones.

Both questions the harness asks are answered by one classifier,
:func:`classify_occurrences`: "should this name be substituted here?"
(:func:`rename_map`) and "is this name still stale?"
(:func:`src.harness_guard.address_map_name_warnings`). They cannot drift apart on
*forms* — article-stripped variants, approved-form masking, hyphen-edged matches
— because there is only one answer for both to read.

Pure functions over ``(AddressMap, Glossary)`` — no I/O, no project layout.
"""

from __future__ import annotations

import re
from typing import Callable, Iterator, NamedTuple

# Straight, right-single, and modifier-letter apostrophes. Drafts mix them, so a
# rule written with one must match text written with another.
_APOSTROPHES = "'’ʼ"
_APOSTROPHE_CLASS = f"[{re.escape(_APOSTROPHES)}]"

# English articles stripped to derive the bare variant of a term ("the
# screech-owl" also matches a bare "screech-owl").
_ENGLISH_ARTICLES: tuple[str, ...] = ("the ", "a ", "an ")

# Target-language definite articles (Spanish). Shared with
# ``harness_guard.glossary_convention_warnings`` via :func:`strip_article`.
_SPANISH_ARTICLES: tuple[str, ...] = ("el ", "la ", "los ", "las ")

# Characters around a match that mean it may be part of a larger name the
# glossary does not carry ("Great-aunt Harriet" when only "Aunt Harriet" is a
# term). Flagged, not suppressed — the human decides.
_COMPOUND_EDGE = "-" + _APOSTROPHES

# How much surrounding text to quote back in a flag.
_CONTEXT_PAD = 30


class Surface(NamedTuple):
    """One text-bearing field of an address map, with a writer back into it."""

    path: str          # precise location, e.g. "pairs[2].directions.a_to_b[0].notes"
    group: str         # coarse bucket for the report: content/global_rules/pairs/rules/...
    text: str
    set: Callable[[str], None]
    prose: bool = True  # False for identity fields (pairs[].a/b) that hold a bare name


class Rule(NamedTuple):
    """One English → target-language substitution derived from the glossary."""

    english: str
    target: str
    source_english: str  # the glossary term this rule came from (== english, or its article-ful form)


class Hit(NamedTuple):
    """One applied substitution, for the report."""

    english: str
    target: str
    group: str
    path: str


# What :func:`classify_occurrences` decided about one match, and therefore what
# both the rename and the stale-cast warning do with it:
#
#   rewrite     plain stale English      substitute it; warn "run the rename"
#   possessive  stale, then an English   substitute it (the name IS wrong) and flag —
#               ``'s`` trails the name   only the trailing ``'s`` needs rewording
#   compound    edged by -/apostrophe    leave it; flag it; warn "needs a hand edit"
#   shadowed    sits inside ANOTHER      leave it; flag it — the glossary contradicts
#               rule's approved form     itself and a human has to pick a form
#   approved    sits inside its OWN      leave it, silently: already reconciled
#               rule's approved form
REWRITTEN_KINDS = ("rewrite", "possessive")
MANUAL_KINDS = ("compound", "shadowed")


class Occurrence(NamedTuple):
    """One rule firing at one site in one text, with what should happen there."""

    rule_index: int
    start: int
    end: int
    kind: str


def iter_surfaces(address_map) -> Iterator[Surface]:
    """Walk every text-bearing field of an ``AddressMap``.

    This is the shared definition of "the map's text". ``rename_map`` rewrites
    what it yields and :func:`src.harness_guard.address_map_name_warnings` builds
    its haystack from it, so the check and the fix can never disagree about which
    fields count — the warning used to read four fields while a hand-rolled
    rename touched nine, which let a stale name survive in a rule's ``notes``
    with nothing reporting it.

    Fields that are ``None`` (an optional never filled in) are skipped; empty
    strings are yielded, since writing into one is harmless.
    """
    am = address_map

    def _field(owner, attr: str, path: str, group: str, prose: bool = True) -> Iterator[Surface]:
        value = getattr(owner, attr, None)
        if value is None:
            return
        yield Surface(path, group, value,
                      lambda v, o=owner, a=attr: setattr(o, a, v), prose)

    yield from _field(am, "content", "content", "content")
    yield from _field(am, "global_rules", "global_rules", "global_rules")
    yield from _field(am, "style_guide_summary", "style_guide_summary", "style_guide_summary")

    for i, pair in enumerate(am.pairs):
        # a/b are identity fields ("canonical/Spanish name", src/models.py) — they
        # hold the approved form verbatim and must not take a sentence-initial capital.
        yield from _field(pair, "a", f"pairs[{i}].a", "pairs", prose=False)
        yield from _field(pair, "b", f"pairs[{i}].b", "pairs", prose=False)
        yield from _field(pair, "relationship", f"pairs[{i}].relationship", "pairs")
        for direction, rules in pair.directions.items():
            for j, rule in enumerate(rules):
                base = f"pairs[{i}].directions.{direction}[{j}]"
                for attr in ("when", "after_event", "notes"):
                    yield from _field(rule, attr, f"{base}.{attr}", "rules")


def _strip_english_article(text: str) -> tuple[str, str]:
    """Split a leading English article off a term. Returns (article, rest)."""
    lowered = text.lower()
    for art in _ENGLISH_ARTICLES:
        if lowered.startswith(art):
            return text[: len(art)].strip(), text[len(art):].strip()
    return "", text.strip()


def strip_article(text: str) -> tuple[str, str]:
    """Split a leading Spanish definite article off a form. Returns (article, rest)."""
    lowered = text.lower()
    for art in _SPANISH_ARTICLES:
        if lowered.startswith(art):
            return text[: len(art)].strip(), text[len(art):].strip()
    return "", text.strip()


def rename_rules(glossary) -> list[Rule]:
    """Derive the substitution rules from the glossary's approved cast.

    Only ``character`` terms: they are what an address map names, and they are
    exactly the set ``address_map_name_warnings`` flags. A term whose approved
    form equals its English (an untranslated name like ``Pollyanna``) yields no
    rule — there is nothing to change.

    Each term also yields an **article-stripped variant** when both sides carry
    one (``the screech-owl`` → ``el tecolote`` also gives ``screech-owl`` →
    ``tecolote``), because map prose says "the screech-owl always uses usted" in
    one sentence and "a screech-owl" or a bare "screech-owl" in the next.

    Sorted **longest English first** — the ordering the single-pass alternation
    relies on so ``Bambi's mother`` wins over ``Bambi`` — with ties broken
    alphabetically so a rename is reproducible.
    """
    seen: dict[str, Rule] = {}
    full_forms: set[str] = set()

    for term in glossary.terms:
        if getattr(term.type, "value", term.type) != "character":
            continue
        english = (term.english or "").strip()
        target = (term.spanish or "").strip()
        if not english or not target or english.casefold() == target.casefold():
            continue
        full_forms.add(english.casefold())
        seen.setdefault(english.casefold(), Rule(english, target, english))

    # Second pass so a stripped variant can never shadow another term's full form
    # ("boys" as a bare variant of "the boys" must not displace a "boys" term).
    for key in list(seen):
        rule = seen[key]
        en_article, en_rest = _strip_english_article(rule.english)
        es_article, es_rest = strip_article(rule.target)
        if not (en_article and es_article and en_rest and es_rest):
            continue
        if en_rest.casefold() in full_forms or en_rest.casefold() == es_rest.casefold():
            continue
        seen.setdefault(en_rest.casefold(), Rule(en_rest, es_rest, rule.english))

    return sorted(seen.values(), key=lambda r: (-len(r.english), r.english.casefold()))


def _rule_source(english: str) -> str:
    """Regex source matching ``english`` literally, tolerant of apostrophe/space form."""
    out: list[str] = []
    for ch in english:
        if ch in _APOSTROPHES:
            out.append(_APOSTROPHE_CLASS)
        elif ch.isspace():
            out.append(r"\s+")
        else:
            out.append(re.escape(ch))
    body = "".join(out)
    # \b only where the edge is a word character; a term edged by punctuation
    # (e.g. a trailing ".") has no word boundary there and \b would never match.
    prefix = r"\b" if english[:1].isalnum() or english[:1] == "_" else ""
    suffix = r"\b" if english[-1:].isalnum() or english[-1:] == "_" else ""
    return prefix + body + suffix


def compile_rules(rules: list[Rule]) -> re.Pattern | None:
    """One alternation over every rule's English, longest first. ``None`` when empty.

    Group *i+1* is rule *i*, so ``match.lastindex`` identifies the rule that fired.

    Matching is **word-bounded** (see :func:`_rule_source`), which is load-bearing
    for the warning as much as the rename: a bare substring test reports ``Thor``
    inside *authority* and ``Eric`` inside *Alberico*, which told the agent to run
    a rename that then correctly refused to touch either — a warning whose
    prescribed fix is a no-op.
    """
    if not rules:
        return None
    return re.compile(
        "|".join(f"({_rule_source(r.english)})" for r in rules),
        re.IGNORECASE,
    )


def _compile_targets(rules: list[Rule]) -> tuple[re.Pattern | None, list[frozenset[int]]]:
    """One alternation over the rules' **approved forms**, longest target first.

    Returns the pattern and, per group, the rule indices that own that target —
    several rules can share one (a term and its article-stripped variant do not,
    but two cast entries approved to the same form would). Deduped case-insensitively
    so the alternation stays as small as the distinct forms.
    """
    ordered: dict[str, tuple[str, set[int]]] = {}
    for i, rule in enumerate(rules):
        text, owners = ordered.setdefault(rule.target.casefold(), (rule.target, set()))
        owners.add(i)
    forms = sorted(ordered.values(), key=lambda t: (-len(t[0]), t[0].casefold()))
    if not forms:
        return None, []
    pattern = re.compile("|".join(f"({_rule_source(t)})" for t, _ in forms), re.IGNORECASE)
    return pattern, [frozenset(owners) for _, owners in forms]


def approved_spans(text: str, rules: list[Rule]) -> list[tuple[int, int, frozenset[int]]]:
    """Where ``text`` already reads an approved form: ``(start, end, owning rules)``.

    Longest form first and single-pass, so ``el detective Smuff`` claims the whole
    span rather than letting a shorter ``el detective`` claim its head and leave
    the tail looking stale.
    """
    pattern, owners = _compile_targets(rules)
    if pattern is None or not text:
        return []
    return [(m.start(), m.end(), owners[(m.lastindex or 1) - 1]) for m in pattern.finditer(text)]


def classify_occurrences(text: str, rules: list[Rule]) -> list[Occurrence]:
    """Every rule firing in ``text``, each labelled with what to do about it.

    The single source of truth for "where does the cast still read English, and
    which of those sites may a machine touch?" — consumed by :func:`rename_map`
    and by :func:`src.harness_guard.address_map_name_warnings`, which is why the
    two can never prescribe a fix the other will not make.

    Non-overlapping and left-to-right over one longest-first alternation: a match
    is consumed and never re-scanned, so ``Bambi's mother`` → ``la madre de Bambi``
    can never then match the ``Bambi`` rule. See :data:`REWRITTEN_KINDS` and the
    table above it for the kinds.
    """
    pattern = compile_rules(rules)
    if pattern is None or not text:
        return []

    spans = approved_spans(text, rules)
    out: list[Occurrence] = []
    for m in pattern.finditer(text):
        idx = (m.lastindex or 1) - 1
        owners = next(
            (o for lo, hi, o in spans if lo <= m.start() and m.end() <= hi), None
        )
        if owners is not None:
            kind = "approved" if idx in owners else "shadowed"
        else:
            before = text[m.start() - 1] if m.start() else ""
            after = text[m.end():m.end() + 2]
            # Possessive first: an apostrophe is also a compound edge, and "'s" is
            # the more specific (and more actionable) reading of it.
            if after[:1] in _APOSTROPHES and after[1:2].lower() == "s":
                kind = "possessive"
            elif (before and before in _COMPOUND_EDGE) or (
                after[:1] and after[0] in _COMPOUND_EDGE
            ):
                kind = "compound"
            else:
                kind = "rewrite"
        out.append(Occurrence(idx, m.start(), m.end(), kind))
    return out


# Sentence enders, the closing punctuation that may sit between one and the next
# capital, and the bullet markers the map's prose uses for its rule lists.
#
# Deliberately excludes ':' and ';'. Neither starts a sentence in English, and
# the map's prose is full of both ("...both approved: (1) Pollyanna...",
# "...the style guide's usted; Miss Polly's coldness..."). Treating them as
# boundaries capitalizes mid-clause, which is a visible error; missing a real
# boundary only leaves a lowercase article, which reads fine.
_SENTENCE_ENDERS = ".!?"
_CLOSERS = "\"'’»)]”"
_BULLETS = "-*•"


def _at_sentence_start(text: str, idx: int) -> bool:
    """Does the match at ``idx`` begin a sentence (or a bullet, or a line)?"""
    j = idx - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0 or text[j] == "\n":
        return True
    if text[j] in _BULLETS and (j == 0 or text[j - 1] in " \t\n"):
        return True
    while j >= 0 and text[j] in _CLOSERS:
        j -= 1
    return j >= 0 and text[j] in _SENTENCE_ENDERS


def _restore_case(replacement: str, sentence_start: bool) -> str:
    """Capitalize a replacement that opens a sentence.

    The approved forms are stored as they read in running prose — narration forms
    lead with a lowercase article (``la tía Ena``, ``el tecolote``). Substituted
    at the head of a sentence that is wrong, and the signal is the **position**,
    not the term's own casing: ``Aunt Ena`` mid-sentence must stay ``la tía Ena``
    while the same term opening a sentence becomes ``La tía Ena``.
    """
    if not sentence_start:
        return replacement
    for i, ch in enumerate(replacement):
        if ch.isalpha():
            return replacement[:i] + ch.upper() + replacement[i + 1:]
        if not ch.isspace():
            break
    return replacement


def context_snippet(text: str, start: int, end: int) -> str:
    """The match plus a little either side, whitespace-collapsed and elided.

    Quoted back by both a rename ``flag`` and the stale-cast warning, so a site
    needing a hand edit can be judged without opening the file.
    """
    lo = max(0, start - _CONTEXT_PAD)
    hi = min(len(text), end + _CONTEXT_PAD)
    frag = " ".join(text[lo:hi].split())
    return ("…" if lo > 0 else "") + frag + ("…" if hi < len(text) else "")


def rename_map(address_map, rules: list[Rule]) -> tuple[list[Hit], list[dict]]:
    """Rewrite ``address_map`` in place, returning ``(hits, flags)``.

    The caller owns copying: pass a ``model_copy(deep=True)`` if the original
    must survive. Each surface is classified once by
    :func:`classify_occurrences` and rebuilt by slicing, so — exactly as with the
    ``re.sub`` it replaces — no replacement is ever re-scanned.

    ``flags`` are dicts the approval gate can present, each
    ``{kind, english, target, path, context}``. Two of the three mark text this
    function **deliberately left in English**, because a deterministic
    substitution cannot decide them:

    * ``compound`` — the match is edged by ``-`` or an apostrophe, so it may be
      part of a longer name the glossary does not carry (``Great-aunt Harriet``
      when only ``Aunt Harriet`` is a term), a possessive-adjective use of a
      common noun (``a 1920s boys' adventure``), or a quoted vocative
      (``'I'm sorry, Uncle Dock'``) that wants the glossary's bare-vocative
      alternative rather than the article-led narration form. Substituting anyway
      produced ``Great-la tía Harriet`` and ``los muchachos' adventure`` — garbage
      a skimming reviewer can miss, in a draft they are being asked to approve.
    * ``shadowed`` — the match sits inside *another* rule's approved form, i.e.
      the glossary contradicts itself (``Aunt Harriet`` → ``la tía Harriet``
      beside ``Harriet`` → ``Enriqueta``). Rewriting would corrupt a reconciled
      phrase; a human has to pick one form.

    The third, ``possessive``, *is* substituted: the name genuinely was stale
    (``Miss Polly's coldness`` → ``la señorita Polly's coldness``) and only the
    trailing English ``'s`` needs rewording afterwards.

    There is deliberately no "the English name survived" flag: whatever remains is
    reported by ``address_map_name_warnings``, which reads the same classifier and
    so names exactly these sites — including the ones left alone here.

    **Idempotent for every rule**, not just self-nested ones: a match lying inside
    an approved form already present in the text is left alone — no substitution,
    no ``Hit``, and no flag when the form is that rule's own (``Detective Smuff``
    inside ``el detective Smuff``). A reconciled map renames to itself and reports
    ``renamed: []``, which is what the skill's "run it again to confirm" loop
    relies on; a compound site left in English re-flags identically on every run.
    """
    if not rules:
        return [], []

    hits: list[Hit] = []
    flags: list[dict] = []

    for surface in iter_surfaces(address_map):
        original = surface.text
        if not original:
            continue

        out: list[str] = []
        cursor = 0
        for occ in classify_occurrences(original, rules):
            if occ.kind == "approved":
                continue  # already the approved form — not stale, not a hit
            rule = rules[occ.rule_index]
            if occ.kind in MANUAL_KINDS or occ.kind == "possessive":
                flags.append({
                    "kind": occ.kind,
                    "english": rule.english,
                    "target": rule.target,
                    "path": surface.path,
                    "context": context_snippet(original, occ.start, occ.end),
                })
            if occ.kind not in REWRITTEN_KINDS:
                continue  # flagged for a human; the English stays put
            out.append(original[cursor:occ.start])
            out.append(_restore_case(
                rule.target, surface.prose and _at_sentence_start(original, occ.start)
            ))
            cursor = occ.end
            hits.append(Hit(rule.source_english, rule.target, surface.group, surface.path))

        if out:
            out.append(original[cursor:])
            surface.set("".join(out))

    return hits, flags
