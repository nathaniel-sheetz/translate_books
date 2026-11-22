"""The over-edit guard for modernize drafts: how much did the model change?

A translation is checked for being *too close* to its source (the echo guard).
A modernization is the opposite: a good draft copies all but a few words, so
the danger is a model that rewrites. This measures the share of the source's
words a draft did not keep, by a word-level diff against the spelling-fixed
source the prompt showed.

Thresholds come from the 2026-11-19 pilot (``exp_v2/A1``, ``A2``: 60 chunks
of *Historia de un cascanueces* on the rules-only prompt): changed words
median 1.2%, max 4.8%; length ratio 0.993–1.005. The reject line sits at
about three times the worst pilot chunk, so only a real rewrite trips it.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

_WORD_RE = re.compile(r"[^\W_]+")

#: A draft that changes more than this share of the source's words is rejected.
REJECT_CHANGED = 0.15
#: ...and one that changes more than this is committed with a warning.
WARN_CHANGED = 0.08
#: Character-length ratio draft/source outside this band is rejected.
LENGTH_BAND = (0.85, 1.15)
#: A run of this many words added or dropped with no counterpart is rejected.
#: Modernizing substitutes in place; in 90 pilot outputs the largest such run
#: was 6 words (once) and every other was at most 2. A duplicated or dropped
#: sentence is what trips it (first harness run, 2026-11-19: a 13-word line
#: repeated at the top of a chunk, under the 15% share).
MAX_BARE_RUN = 8
#: A paragraph of at least PARA_MIN_WORDS words changed beyond PARA_WARN is named.
PARA_WARN = 0.30
PARA_MIN_WORDS = 12


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def changed_share(source: str, draft: str) -> float:
    """Share of the source's words the draft did not keep, in order (0..1)."""
    a, b = _words(source), _words(draft)
    if not a:
        return 0.0 if not b else 1.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    kept = sum(block.size for block in sm.get_matching_blocks())
    # A swapped word is one dropped and one added; take the larger side so a
    # padded draft cannot hide behind a high keep rate. Capped at 1.
    dropped, added = len(a) - kept, len(b) - kept
    return min(1.0, max(dropped, added) / len(a))


def bare_runs(source: str, draft: str) -> list[tuple[str, str]]:
    """``(kind, words)`` for each run of MAX_BARE_RUN+ words added or dropped outright."""
    a, b = _words(source), _words(draft)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    runs = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "insert" and j2 - j1 >= MAX_BARE_RUN:
            runs.append(("added", " ".join(b[j1:j2])))
        elif op == "delete" and i2 - i1 >= MAX_BARE_RUN:
            runs.append(("dropped", " ".join(a[i1:i2])))
    return runs


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


@dataclass
class OverEditReport:
    changed: float
    length_ratio: float
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def over_edit_report(source: str, draft: str) -> OverEditReport:
    """Measure ``draft`` against ``source`` (both spelling-fixed)."""
    changed = changed_share(source, draft)
    ratio = len(draft.strip()) / max(len(source.strip()), 1)
    rep = OverEditReport(changed=changed, length_ratio=ratio)
    if changed > REJECT_CHANGED:
        rep.problems.append(
            f"over-edit: {changed:.0%} of the source's words changed "
            f"(>{REJECT_CHANGED:.0%}); a modernization keeps all but a few"
        )
    elif changed > WARN_CHANGED:
        rep.warnings.append(f"{changed:.0%} of the source's words changed (warn above {WARN_CHANGED:.0%})")
    for kind, words in bare_runs(source, draft):
        head = " ".join(words.split()[:10])
        rep.problems.append(
            f"{kind} {len(words.split())} words with no counterpart in the "
            f"{'source' if kind == 'added' else 'draft'}: «{head}…»"
        )
    lo, hi = LENGTH_BAND
    if not lo <= ratio <= hi:
        rep.problems.append(
            f"length ratio {ratio:.2f} outside {lo:.2f}–{hi:.2f}: text dropped or added"
        )
    src_paras, out_paras = _paragraphs(source), _paragraphs(draft)
    # Paragraph warnings only when the paragraphs line up one to one; a split
    # for dialogue shifts them, and the chunk-level numbers still hold.
    if len(src_paras) == len(out_paras):
        for i, (sp, op) in enumerate(zip(src_paras, out_paras), 1):
            if len(_words(sp)) >= PARA_MIN_WORDS:
                share = changed_share(sp, op)
                if share > PARA_WARN:
                    head = " ".join(sp.split()[:8])
                    rep.warnings.append(f"paragraph {i} changed {share:.0%}: «{head}…»")
    return rep
