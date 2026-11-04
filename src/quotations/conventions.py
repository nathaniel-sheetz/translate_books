"""
The NBLA's conventions, as pure functions over strings.

Taken from ``~/.claude/skills/nbla-bible-mapping`` (confirmed with the user on
short-sermons) and :mod:`src.quotations.index`'s line markers:

- ``SEÑOR`` / ``DIOS`` for the divine name, and the NBLA's reverential capitals
  (Su, Mi, Tú, Él…), are kept as the NBLA prints them.
- All-capital Old Testament passages become sentence case. The NBLA index
  already does this from the EPUB's small-caps markup;
  :func:`sentence_case_caps` is the fallback for text that arrives in capitals.
- The first word's capital follows its place in the Spanish sentence.
- **Inline lowercasing.** A quotation is *inline* when it does not fill its
  paragraph. Only then, the capital at each indexed poetry line start inside
  the quotation is lowered, except the quotation's first word, a word after
  ``. ! ?``, a reverential capital, ``SEÑOR`` / ``DIOS`` and a name. Capitals
  anywhere else are never touched, so "Jerusalén" stays as it is.
- Verse numbers and line markers never reach the book.
- References are written ``Jn 10:5`` with the book's own range separator
  (:meth:`src.quotations.refs.Ref.spanish`).
- The book's « » and ``_…_`` epigraph markers around a quotation are kept:
  :func:`splice` replaces only what sits inside them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from src.quotations.index import LINE

DIVINE_NAMES = frozenset({"SEÑOR", "DIOS"})

#: The NBLA's reverential capitals, kept wherever they fall.
REVERENTIAL = frozenset({
    "Su", "Sus", "Mi", "Mis", "Tu", "Tus", "Tú", "Te", "Ti", "Yo", "Él", "Aquel",
})

#: Reverentials that also occur, uncapitalised, for people in the same verses;
#: at a line start their capital is checked against that usage.
POSSESSIVES = frozenset({"Su", "Sus", "Mi", "Mis", "Tu", "Tus"})

SENTENCE_END = ".!?"
OPENERS = "«“‘\"'¡¿(_—–-"
CLOSERS = "»”’\"'_)"
QUOTE_PAIRS = (("«", "»"), ("“", "”"), ("‘", "’"))

_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+")


# ── locating a piece in the indexed verses ──────────────────────────────────

@dataclass
class Located:
    """A piece found in the concatenated verse text."""

    text: str                 # the piece as indexed (markers removed)
    line_starts: list[int]    # offsets in ``text`` where a poetry line begins
    verse_keys: list[str]     # the verses the piece touches
    start: int = 0            # where it sits in ``source``
    end: int = 0
    source: str = ""          # every verse of the reference, flattened
    source_starts: frozenset[int] = frozenset()  # line starts in ``source``


def _flatten(verses: list[tuple[str, str]]) -> tuple[str, set[int], list[tuple[int, str]]]:
    """``(plain, line_start_offsets, [(offset, verse_key)])`` for verses in order."""
    out: list[str] = []
    starts: set[int] = set()
    bounds: list[tuple[int, str]] = []
    for key, text in verses:
        if out and out[-1] != " ":
            out.append(" ")
        bounds.append((len(out), key))
        pending = False
        for ch in text:
            if ch == LINE:
                pending = True
                if out and out[-1] != " ":
                    out.append(" ")
                continue
            if ch.isspace():
                if out and out[-1] != " ":
                    out.append(" ")
                continue
            if pending:
                starts.add(len(out))
                pending = False
            out.append(ch)
    plain = "".join(out).strip()
    lead = len("".join(out)) - len("".join(out).lstrip())
    return plain, {s - lead for s in starts}, [(max(0, b - lead), k) for b, k in bounds]


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def locate(verses: list[tuple[str, str]], piece: str) -> Optional[Located]:
    """Find ``piece`` verbatim in ``verses`` (``[(key, marked_text)]``), else ``None``.

    Whitespace is normalised; nothing else is. A first letter in the other case
    is accepted, since a worker quoting mid-verse often lowercases it and the
    conventions decide that capital anyway.
    """
    want = normalize_ws(piece)
    if not want:
        return None
    plain, starts, bounds = _flatten(verses)
    at = plain.find(want)
    if at < 0 and want[0].isalpha():
        swapped = want[0].swapcase() + want[1:]
        at = plain.find(swapped)
    if at < 0:
        return None
    end = at + len(want)
    keys = [k for i, (b, k) in enumerate(bounds)
            if b < end and (i + 1 == len(bounds) or bounds[i + 1][0] > at)]
    return Located(
        text=plain[at:end],
        line_starts=sorted(s - at for s in starts if at <= s < end),
        verse_keys=keys,
        start=at,
        end=end,
        source=plain,
        source_starts=frozenset(starts),
    )


# ── capitals ─────────────────────────────────────────────────────────────────

def _protected(word: str, proper: frozenset[str]) -> bool:
    return word in DIVINE_NAMES or word in REVERENTIAL or word.lower() in proper


def sentence_case_caps(text: str, proper: Iterable[str] = ()) -> str:
    """Lower runs of two or more all-capital words ("ESTOY ATERRADO").

    ``SEÑOR`` / ``DIOS`` alone are not a run and are kept inside one. A run's
    first word keeps its capital when it starts a sentence; a known name keeps
    its capital anywhere.
    """
    proper = frozenset(w.lower() for w in proper)
    words = list(_WORD_RE.finditer(text))
    in_run: set[int] = set()
    i = 0
    while i < len(words):
        j = i
        while j < len(words) and (words[j].group().isupper()):
            j += 1
        run = words[i:j]
        real = [m for m in run if m.group() not in DIVINE_NAMES and len(m.group()) > 1]
        if len(run) >= 2 and len(real) >= 1 and sum(1 for m in run if len(m.group()) > 1) >= 2:
            in_run.update(range(i, j))
        i = max(j, i + 1)
    out = list(text)
    for idx in sorted(in_run):
        m = words[idx]
        word = m.group()
        if word in DIVINE_NAMES:
            continue
        lowered = word.lower()
        before = text[: m.start()].rstrip().rstrip(OPENERS).rstrip()
        keep_cap = lowered in proper or not before or before[-1] in SENTENCE_END
        new = (lowered[0].upper() + lowered[1:]) if keep_cap else lowered
        out[m.start() : m.end()] = list(new)
    return "".join(out)


def _first_word(text: str, start: int = 0) -> Optional[re.Match[str]]:
    return _WORD_RE.search(text, start)


def _prev_char(text: str, pos: int) -> str:
    """The last non-space, non-opener character before ``pos``."""
    before = text[:pos].rstrip()
    while before and before[-1] in OPENERS:
        before = before[:-1].rstrip()
    return before[-1] if before else ""


def _reverential_here(word: str, context: str, context_starts: frozenset[int]) -> bool:
    """Whether a line-initial pronoun is the NBLA's reverential capital.

    At a line start every word is capitalised, so the capital proves nothing.
    The same verses' other uses of the word, away from line and sentence starts,
    decide: "has ungido mi cabeza… Mi copa" (Ps 23:5) is the psalmist's "mi";
    "Tu vara y Tu cayado" (Ps 23:4) is God's. With no other use, the capital stays.
    """
    if not context:
        return True
    capital = lower = 0
    pattern = rf"(?<![\wÁÉÍÓÚÑÜáéíóúñü]){re.escape(word.lower())}(?![\wÁÉÍÓÚÑÜáéíóúñü])"
    for match in re.finditer(pattern, context, re.IGNORECASE):
        prev = _prev_char(context, match.start())
        if match.start() in context_starts or not prev or prev in SENTENCE_END:
            continue
        if match.group()[0].isupper():
            capital += 1
        else:
            lower += 1
    return not (lower and not capital)


def lower_line_starts(
    text: str,
    line_starts: list[int],
    proper: Iterable[str] = (),
    *,
    context: str = "",
    context_starts: frozenset[int] = frozenset(),
) -> str:
    """Lower the capital at each poetry line start, as an inline quotation needs.

    Offset 0 (the quotation's first word) is left for :func:`set_first_capital`.
    ``context`` is the flattened text of all the reference's verses: the
    evidence for a line-initial pronoun (:func:`_reverential_here`).
    """
    proper = frozenset(w.lower() for w in proper)
    chars = list(text)
    for start in line_starts:
        if start <= 0:
            continue
        match = _first_word(text, start)
        if not match or text[start : match.start()].strip(OPENERS + " "):
            continue
        word = match.group()
        if not word[0].isupper() or word.isupper() and len(word) > 1:
            continue
        if word in POSSESSIVES:
            if _reverential_here(word, context, context_starts):
                continue
        elif _protected(word, proper):
            continue
        if _prev_char(text, match.start()) in SENTENCE_END:
            continue
        chars[match.start()] = word[0].lower()
    return "".join(chars)


def set_first_capital(text: str, *, sentence_initial: bool, proper: Iterable[str] = ()) -> str:
    """Capitalise the first word, or lower it when the quotation runs on mid-sentence."""
    proper = frozenset(w.lower() for w in proper)
    match = _first_word(text)
    if not match:
        return text
    word = match.group()
    if sentence_initial:
        new = word[0].upper()
    elif _protected(word, proper) or (word.isupper() and len(word) > 1):
        return text
    else:
        new = word[0].lower()
    return text[: match.start()] + new + text[match.start() + 1 :]


# ── punctuation and wrappers ─────────────────────────────────────────────────

def strip_unbalanced_quotes(text: str) -> str:
    """Drop the NBLA's own speech marks when a piece cuts through them.

    A piece taken from inside a speech carries a lone » or “ (Jn 10:5 ends
    "…de los extraños»"); inside the book's own « » that would be a stray mark.
    """
    for left, right in QUOTE_PAIRS:
        keep: list[str] = []
        unmatched_close: list[int] = []
        opens: list[int] = []
        for ch in text:
            if ch == left:
                opens.append(len(keep))
            elif ch == right:
                if opens:
                    opens.pop()
                else:
                    unmatched_close.append(len(keep))
            keep.append(ch)
        drop = set(opens) | set(unmatched_close)
        text = "".join(ch for i, ch in enumerate(keep) if i not in drop)
    return re.sub(r"\s{2,}", " ", text).strip()


_TRAIL_RE = re.compile(r"[\s.,;:!?…»”’\"'_)\]]*$")
_LEAD_RE = re.compile(r"^[\s«“‘\"'_(\[¡¿—–-]*")


def split_wrappers(old: str) -> tuple[str, str, str]:
    """``(lead, inner, trail)``: the book's own marks around a quotation."""
    lead = _LEAD_RE.match(old).group()
    rest = old[len(lead) :]
    trail = _TRAIL_RE.search(rest).group()
    inner = rest[: len(rest) - len(trail)] if trail else rest
    # Keep "¡" / "¿" with the text when the text keeps its "!" / "?".
    return lead, inner, trail


def finish(new_inner: str, old_trail: str, *, opens_with: str = "") -> tuple[str, str]:
    """Settle the end punctuation between the NBLA text and the book's.

    The NBLA's own ``? !`` stay with it. Its ``, ; :`` and ``.`` give way to
    whatever the book had after the quotation, so a quotation cut mid-verse
    does not end the book's sentence on a semicolon.
    """
    stripped = new_inner.rstrip()
    end = re.search(r"[.,;:…]+$", stripped)
    if end:
        stripped = stripped[: end.start()].rstrip()
    for opener, closer in (("¿", "?"), ("¡", "!")):
        if stripped.endswith(closer) and opener not in stripped and not opens_with:
            # The question began before the words quoted ("…y que ustedes no
            # caminan solos?"): the fragment is a statement in the book.
            return stripped[:-1].rstrip(), old_trail
    if stripped.endswith(("?", "!")):
        trail = re.sub(r"^[\s.?!]+", "", old_trail)
        return stripped, trail
    return stripped, old_trail


def is_sentence_initial(before: str) -> bool:
    """True when the text before a quotation leaves it starting a sentence.

    A reference in parentheses between the two ("…cuenta.» (He 4:13) «Desnudo")
    is looked through: it belongs to the quotation before.
    """
    tail = before.rstrip()
    while True:
        while tail and tail[-1] in OPENERS:
            tail = tail[:-1].rstrip()
        if tail.endswith(")") and "(" in tail:
            tail = tail[: tail.rfind("(")].rstrip()
            continue
        break
    while tail and tail[-1] in CLOSERS and tail[-1] != ")":
        tail = tail[:-1].rstrip()
    return not tail or tail[-1] in SENTENCE_END + ":"


# ── one quotation, end to end ────────────────────────────────────────────────

def _book_line_capitals(text: str, lowered: str, starts: list[int], old: str) -> str:
    """A block quotation's line capitals, as the book already writes them.

    The NBLA's capital stays unless the translator's text has that line's
    first word in lower case and never capitalised: then the book's choice
    stands (short-sermons lowers them in some set-off verses, not others).
    """
    if not old or text == lowered:
        return text
    chars = list(text)
    for start in starts:
        if start <= 0 or start >= len(text) or text[start] == lowered[start]:
            continue
        match = _WORD_RE.match(text, start)
        if not match:
            continue
        word = match.group()
        low = word[0].lower() + word[1:]
        if re.search(rf"(?<!\w){re.escape(low)}(?!\w)", old) and not re.search(
            rf"(?<!\w){re.escape(word)}(?!\w)", old
        ):
            chars[start] = lowered[start]
    return "".join(chars)


_QUOTE_MARKS = "«»“”‘’‹›\""


def join_separator(prev: Located, nxt: Located, old: str) -> tuple[str, bool]:
    """``(separator, contiguous)`` between two pieces of one quotation.

    Pieces that sit side by side in the NBLA (split at a verse boundary, or
    around a speech mark) are joined with the NBLA's own punctuation between
    them: "Sea el día" + "Y hubo día" reads "Sea el día. Y hubo día". Where the
    author skipped words, the book's own ellipsis is kept if it used one;
    otherwise a piece that ends without punctuation takes the book's ";" or ",".
    """
    if prev.source and prev.source == nxt.source and nxt.start >= prev.end:
        between = prev.source[prev.end : nxt.start]
        if not re.search(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9]", between):
            # Across a speech mark the next capital is the mark's, not the
            # sentence's: report it as not contiguous so it is lowered unless
            # a full stop precedes it.
            marked = any(c in _QUOTE_MARKS for c in between)
            between = normalize_ws("".join(c for c in between if c not in _QUOTE_MARKS))
            return (between + " " if between else " "), not marked
    if "…" in old or "..." in old:
        return "… ", False
    if prev.text.rstrip()[-1:] in ".,;:!?…":
        return " ", False
    return ("; " if ";" in old else ", "), False


def render_quotation(
    located: list[Located],
    *,
    inline: bool,
    sentence_initial: bool,
    proper: Iterable[str] = (),
    old: str = "",
    fix_caps: bool = True,
) -> str:
    """The NBLA text for one span of the book, conventions applied.

    ``located`` is the piece or pieces the author quotes, in order, joined by
    :func:`join_separator`. ``old`` is the translator's text being replaced.
    ``fix_caps=False`` skips :func:`sentence_case_caps` for an index that read
    the small caps from markup, where any capitals left are the version's own.
    """
    proper = frozenset(w.lower() for w in proper)
    text = ""
    starts: list[int] = []
    for i, loc in enumerate(located):
        if i:
            sep, contiguous = join_separator(located[i - 1], loc, old)
            text = text.rstrip()
            if sep.startswith("…"):
                text = text.rstrip(".,;:")
            text += sep
            if loc.text[:1] in ",;:.":
                text = text.rstrip()
            if not contiguous:
                # A later run of words reads like a new line: its capital is
                # lowered unless it follows a sentence end.
                starts.append(len(text))
        starts.extend(len(text) + s for s in loc.line_starts)
        text += loc.text
    if fix_caps:
        text = sentence_case_caps(text, proper)
    first = located[0] if located else None
    lowered = lower_line_starts(
        text, starts, proper,
        context=first.source if first else "",
        context_starts=first.source_starts if first else frozenset(),
    )
    if inline:
        text = lowered
    else:
        text = _book_line_capitals(text, lowered, starts, old)
    text = strip_unbalanced_quotes(text)
    return set_first_capital(text, sentence_initial=sentence_initial, proper=proper)


def splice(old: str, nbla_inner: str) -> str:
    """``old`` with its inner text replaced by ``nbla_inner``; wrappers kept."""
    lead, _inner, trail = split_wrappers(old)
    body, trail = finish(nbla_inner, trail, opens_with=lead[-1:] if lead[-1:] in "¿¡" else "")
    # An exclamation or question the book added and the NBLA lacks goes, both marks.
    if lead.endswith("¡") and not body.endswith("!"):
        lead = lead[:-1]
        trail = trail.replace("!", "", 1)
    if lead.endswith("¿") and not body.endswith("?"):
        lead = lead[:-1]
        trail = trail.replace("?", "", 1)
    for opener, closer in (("¿", "?"), ("¡", "!")):
        if body.startswith(opener) and lead.endswith(opener):
            lead = lead[:-1]
        elif body.endswith(closer) and opener not in body and not lead.endswith(opener):
            body = opener + body
    return lead + body + trail


def fills_paragraph(paragraph: str, old: "str | list[str]") -> bool:
    """True when ``old`` is the whole paragraph bar marks and a reference.

    Such a quotation is a set-off block and keeps the NBLA's line capitals.
    ``old`` may be several strings: the rows of one quotation that share the
    paragraph.
    """
    from src.quotations.refs import find_es_refs

    olds = [old] if isinstance(old, str) else list(old)
    if not olds or any(o not in paragraph for o in olds):
        return False
    rest = paragraph
    for piece in olds:
        rest = rest.replace(piece, " ", 1)
    for found in reversed(find_es_refs(rest)):
        rest = rest[: found.start] + rest[found.end :]
    return not re.search(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü]", rest)
