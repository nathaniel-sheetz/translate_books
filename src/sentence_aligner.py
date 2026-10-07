"""
Sentence-level alignment between source and translated text.

Uses pysbd for sentence boundary detection, a post-split step for
oversized sentences (common in English literary dialogue), and
sentence-transformers embeddings with monotonic dynamic programming
to find the best alignment.

Around the DP sit three repairs for the ways the two languages split
differently: Spanish fragments are glued into units (_glue_units), the
source side is split inside quotations (_split_inside_quotes), and a row
may take in an unclaimed source sentence from its own paragraph
(_absorb_orphans).
"""

import json
import re
from pathlib import Path
from typing import Optional

import numpy as np
import pysbd

from src.utils.verse import is_verse_block

# Lazy-loaded to avoid slow import when not needed
_model = None

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
MAX_SENTENCE_WORDS = 50
HIGH_CONFIDENCE_THRESHOLD = 0.5
SKIP_PENALTY = 0.05

# Coverage-gap reporting (see _coverage_gaps). Calibrated by re-aligning all 522
# translated chunks across 18 books: qualifying orphan-run mass is 0 for ~96% of
# chunks, and there is an empty band between 300 and 499 chars before the real
# omissions start. At 300 the scan flagged 10 chunks, every one a genuine drop.
MIN_GAP_CHARS = 300
# Sentence splitting emits standalone junk records for Gutenberg rules ("---"),
# stray quote marks and verse punctuation. They are never "translated", so they
# must not contribute to a gap's mass.
MIN_SENTENCE_CHARS = 25
# Only complete sentences count toward a gap. Some sources are hard-wrapped at
# ~70 columns, and is_verse_block reads those prose paragraphs as verse and
# splits them per line — so one translated Spanish sentence can face seven
# English line *fragments*, whose combined mass would otherwise clear
# MIN_GAP_CHARS even though nothing was dropped.
# 1:N absorption (see _absorb_orphans). A longer run is left unclaimed, and so is
# any run heavy enough for _coverage_gaps to report — a dropped paragraph must
# never be folded into a neighbour and disappear.
MAX_ABSORB_SENTENCES = 2
SENTENCE_TERMINALS = ".!?…"
SENTENCE_CLOSERS = "\"'”’»)]"


def _get_model():
    """Lazy-load the sentence-transformers model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _normalize_for_embedding(text: str) -> str:
    """
    Lowercase sentences that are entirely uppercase (chapter titles).

    The embedding model (paraphrase-multilingual-MiniLM-L12-v2) is trained on
    mixed-case text; all-caps inputs like "KING ALFRED AND THE CAKES." tokenize
    poorly and produce similarity scores in the 0.15-0.55 range even for
    perfect translations. Lowercasing yields in-distribution tokens without
    changing semantics. Sentences containing any lowercase letter (including
    acronyms like "the USA") are left alone.
    """
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 3 and all(c.isupper() for c in letters):
        return text.lower()
    return text


# Two fixed-width lookbehind branches so we can split on either
# `.!?` directly before whitespace OR `.!?` followed by a closing
# quote/bracket before whitespace, WITHOUT consuming the closing
# quote/bracket. (Python `re` lookbehind requires fixed width per
# alternative.)
_SPLIT_LONG_RE = re.compile(
    r"(?:(?<=[.!?])|(?<=[.!?][\"'\u201D\u2019\u00BB\)\]]))"
    r"\s+(?=[A-Z\u00BF\u00A1'\"\u201C\u2018\u00AB\(\[])"
)

# A "run-on" pattern: sentence-ender, optional closing quote/bracket,
# whitespace, then an OPENING quote or parenthesis. This is the
# signature of strung-together quotations or quote+(reference)+quote
# runs that pysbd commonly fails to split. Unlike the broader
# _SPLIT_LONG_RE (which also accepts a capital letter after the space),
# this is restrictive enough to be safe to trigger on short sentences
# without false-positives on abbreviations like "Dr. Smith".
_RUN_ON_RE = re.compile(
    r"[.!?][\"'\u201D\u2019\u00BB\)\]]?\s+[\"\u201C\u2018\u00AB\(\[]"
)


# English titles that end in a period without ending a sentence. pysbd knows
# them, but _SPLIT_LONG_RE does not, and it cut "Mrs. | Dorking" 158 times across
# the corpus. English only: the Spanish split is load-bearing (es_idx anchors
# annotations and corrections) and must stay byte-identical.
_EN_TITLE_ABBREV_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|St|Messrs|Capt|Col|Gen|Rev|Prof|Jr|Sr)\.$"
)


def _split_long_sentence(text: str, language: Optional[str] = None) -> list[str]:
    """
    Split a long sentence on sentence-ending punctuation (optionally
    followed by a closing quote/bracket) and whitespace, before an
    uppercase letter or opening quote/bracket. Fixes pysbd's tendency
    to treat entire quoted dialogue passages as single sentences,
    including the common case `."  "Next…` where the closing quote
    sits between the period and the whitespace.

    With ``language="en"`` a boundary straight after a title abbreviation
    ("Mr. Hardy") is not a boundary.
    """
    parts = []
    start = 0
    for m in _SPLIT_LONG_RE.finditer(text):
        if language == "en" and _EN_TITLE_ABBREV_RE.search(text[: m.start()]):
            continue
        parts.append(text[start : m.start()])
        start = m.end()
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def split_sentences(text: str, language: str) -> list[str]:
    """
    Split text into sentences using pysbd, then post-split any sentence
    that is either longer than MAX_SENTENCE_WORDS or contains a run-on
    quotation pattern (period+optional-close+space+opening-quote/paren),
    which pysbd routinely fails to break apart.
    """
    segmenter = pysbd.Segmenter(language=language, clean=False)
    raw_sentences = segmenter.segment(text)

    result = []
    for sent in raw_sentences:
        sent = sent.strip()
        if not sent:
            continue
        needs_split = (
            len(sent.split()) > MAX_SENTENCE_WORDS
            or _RUN_ON_RE.search(sent) is not None
        )
        if needs_split:
            sub_sents = _split_long_sentence(sent, language)
            if len(sub_sents) > 1:
                result.extend(sub_sents)
            else:
                result.append(sent)
        else:
            result.append(sent)

    return result


# A sentence boundary with nothing but whitespace between the terminal
# punctuation and the next capital. Where a closing quote intervenes pysbd has
# already split, so this only ever matches *inside* a quotation.
_INNER_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-Z])")
# "J. B. Smith", "U. S." — an initial is not the end of a sentence.
_INITIAL_RE = re.compile(r"\b[A-Z]\.$")


def _split_inside_quotes(sentences: list[str]) -> list[str]:
    """
    Split the sentences of one paragraph at boundaries inside a quotation.

    pysbd protects quoted text, so '"Yeah. Three fellers. Sort of onpleasant
    lookin\\' chaps."' comes back as one record, while the Spanish — raya
    dialogue with no closing mark — splits into three. A short '—Sí.' then
    faces a whole speech and lands on whichever neighbour scores a hair higher.

    Quote state is carried across the paragraph's sentences (pysbd sometimes
    cuts mid-quotation) and reset at the paragraph, which is where the
    continued-quotation convention reopens it. Only double quotes are tracked;
    a single quote cannot be told from an apostrophe.
    """
    out: list[str] = []
    straight_open = False
    curly_depth = 0
    for sent in sentences:
        boundaries = {m.start(): m.end() for m in _INNER_BOUNDARY_RE.finditer(sent)}
        start = 0
        for pos, ch in enumerate(sent):
            if pos in boundaries and (straight_open or curly_depth > 0):
                before = sent[:pos]
                if not (_EN_TITLE_ABBREV_RE.search(before) or _INITIAL_RE.search(before)):
                    out.append(sent[start:pos])
                    start = boundaries[pos]
            if ch == '"':
                straight_open = not straight_open
            elif ch == "“":
                curly_depth += 1
            elif ch == "”":
                curly_depth = max(0, curly_depth - 1)
        out.append(sent[start:])
    return [s.strip() for s in out if s.strip()]


def _split_sentences_with_para_indices(
    text: str, language: str, split_quotes: bool = False
) -> tuple[list[str], list[int]]:
    """
    Split multi-paragraph text into sentences, tracking which paragraph
    each sentence came from.

    ``split_quotes`` also splits prose sentences inside quotations (see
    _split_inside_quotes). It is for the aligner's source side only: the
    target split is load-bearing — the reader re-splits a chunk with this
    function and maps alignment rows onto the result by position.

    Verse paragraphs (per is_verse_block) are split on '\\n' BEFORE
    pysbd so each verse line becomes its own sentence record. Without
    this, pysbd's terminal-punctuation heuristic produces inconsistent
    line-level granularity for poetry (a line ending in ',' is joined
    with the next; a line ending in '.' is split) which makes
    downstream verse-line preservation in the reader unreliable.

    Returns (sentences, para_indices) where para_indices[i] is the
    zero-based paragraph number for sentence i.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    sentences: list[str] = []
    para_indices: list[int] = []
    for para_idx, para in enumerate(paragraphs):
        if is_verse_block(para):
            for line in para.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Use the verse line as-is — pysbd on a single line could split
                # "He sang. Softly." into two records, breaking the
                # 1-record-per-line invariant that downstream alignment depends on.
                sentences.append(line)
                para_indices.append(para_idx)
        else:
            para_sents = split_sentences(para, language)
            if split_quotes:
                para_sents = _split_inside_quotes(para_sents)
            sentences.extend(para_sents)
            para_indices.extend([para_idx] * len(para_sents))
    return sentences, para_indices


# A target sentence that opens with a raya and a lowercase letter is a narrator's
# inciso ("—exclamó—.") that pysbd cut off the line it belongs to. English keeps
# the pair whole ('"Grandpa!" he cried.'), so on its own the fragment has nothing
# to match and lands on whichever neighbour scores a hair higher.
_ES_INCISO_RE = re.compile(r"^[—–]\s*[a-záéíóúüñ]")
# Spanish titles pysbd splits after ("El Sr." | "Hardy seguía…").
_ES_TITLE_ABBREV_RE = re.compile(r"\b(?:Sr|Sra|Srta|Sres|Dr|Dra|Mr|Mrs)\.$")
# No sentence opens with an ellipsis, comma, semicolon or colon: pysbd cut after a
# mid-sentence "!" or "?" ("—¡Juuu!" | "... ¡Ja!" | "... ¡Ja!"). Left apart, each
# crumb adds its own term to the DP's sum and a run of them can outvote the real
# sentences around it. (A lowercase start is deliberately not a signal here: that
# is what a verse line looks like.)
_ES_CONTINUATION_RE = re.compile(r"^(?:\.\.\.|…|[,;:])")


def _glue_units(
    sentences: list[str],
    para_indices: list[int] | None = None,
) -> list[list[int]]:
    """
    Group target sentences that must be aligned as one unit.

    Returns runs of consecutive sentence indices covering every sentence once.
    A sentence joins the unit before it when it is a narrator's inciso, when it
    opens with continuation punctuation, or when the previous sentence stops at
    a title abbreviation — all artefacts of the Spanish split, which cannot
    itself change (es_idx anchors annotations
    and corrections, and the reader re-splits the chunk live). Gluing here
    leaves every index where it was and only makes the pieces share a source
    sentence. Never glues across a paragraph boundary.
    """
    units: list[list[int]] = []
    for i, sent in enumerate(sentences):
        same_para = i > 0 and (
            para_indices is None or para_indices[i] == para_indices[i - 1]
        )
        if same_para and (
            _ES_INCISO_RE.match(sent)
            or _ES_CONTINUATION_RE.match(sent)
            or _ES_TITLE_ABBREV_RE.search(sentences[i - 1])
        ):
            units[-1].append(i)
        else:
            units.append([i])
    return units


def _monotonic_alignment(
    similarity: np.ndarray,
) -> list[tuple[int, int, float]]:
    """
    Find the best monotonically non-decreasing alignment between
    target sentences (rows) and source sentences (columns) using DP.

    Each target sentence maps to exactly one source sentence.
    Source sentences can be skipped or shared (many-to-one).

    Uses a prefix-max trick to avoid the inner k-loop, reducing
    complexity from O(n_tgt * n_src^2) to O(n_tgt * n_src).
    """
    n_tgt, n_src = similarity.shape

    dp = np.full((n_tgt, n_src), -np.inf)
    backtrack = np.full((n_tgt, n_src), -1, dtype=int)

    # Base case
    for j in range(n_src):
        dp[0][j] = similarity[0][j]

    # Fill — for each (i, j), best previous k falls into three cases:
    #   k < j-1: skip penalty applies, use prefix-max of
    #            dp[i-1][k] + (k+1)*SKIP_PENALTY, subtract j*SKIP_PENALTY
    #   k = j-1: skipped=0, no penalty
    #   k = j:   same source sentence, no penalty
    for i in range(1, n_tgt):
        pmax_val = -np.inf  # prefix max of val[k] for k in [0, j-2]
        pmax_k = -1

        for j in range(n_src):
            best_score = -np.inf
            best_k = -1

            # Case 1: k in [0, j-2] with skip penalty
            if j >= 2 and pmax_val > -np.inf:
                score = pmax_val - j * SKIP_PENALTY
                if score > best_score:
                    best_score = score
                    best_k = pmax_k

            # Case 2: k = j-1, no penalty
            if j >= 1:
                score = dp[i - 1][j - 1]
                if score > best_score:
                    best_score = score
                    best_k = j - 1

            # Case 3: k = j (many-to-one), no penalty
            score = dp[i - 1][j]
            if score > best_score:
                best_score = score
                best_k = j

            dp[i][j] = best_score + similarity[i][j]
            backtrack[i][j] = best_k

            # Extend prefix max to include k = j-1 for next iteration
            if j >= 1:
                val = dp[i - 1][j - 1] + j * SKIP_PENALTY
                if val > pmax_val:
                    pmax_val = val
                    pmax_k = j - 1

    # Backtrack
    best_end = int(np.argmax(dp[n_tgt - 1]))
    alignment = []
    j = best_end
    for i in range(n_tgt - 1, -1, -1):
        alignment.append((i, j, float(similarity[i][j])))
        j = backtrack[i][j]

    alignment.reverse()
    return alignment


def align_sentences(
    en_sentences: list[str],
    es_sentences: list[str],
    model=None,
    es_para_indices: list[int] | None = None,
    en_para_indices: list[int] | None = None,
) -> list[dict]:
    """
    Align Spanish sentences to English sentences using embedding
    similarity with monotonic constraint.

    Returns a list of alignment records, one per Spanish sentence:
        {
            "es_idx": int,
            "en_idx": int,
            "es": str,
            "en": str,
            "similarity": float,
            "confidence": "high" | "low"
        }

    es_para_indices: optional list of paragraph numbers per ES sentence.
        When provided, N:1 grouping is blocked across paragraph boundaries.
    en_para_indices: optional list of paragraph numbers per EN sentence.
        When provided, a row absorbs short unclaimed runs of EN sentences
        from its own source paragraph (see _absorb_orphans); without it
        every row keeps exactly one EN sentence.
    """
    if not en_sentences or not es_sentences:
        return []

    if model is None:
        model = _get_model()

    # The DP runs over glued units, then every sentence in a unit takes the
    # unit's source sentence; _group_nto1 folds them back into one row.
    units = _glue_units(es_sentences, es_para_indices)
    en_for_embed = [_normalize_for_embedding(s) for s in en_sentences]
    es_for_embed = [
        _normalize_for_embedding(" ".join(es_sentences[i] for i in unit))
        for unit in units
    ]

    en_embeddings = model.encode(en_for_embed, normalize_embeddings=True)
    es_embeddings = model.encode(es_for_embed, normalize_embeddings=True)

    similarity = np.dot(es_embeddings, en_embeddings.T)
    raw_alignment = [
        (es_idx, en_idx, score)
        for unit_idx, en_idx, score in _monotonic_alignment(similarity)
        for es_idx in units[unit_idx]
    ]

    unit_of = [unit_idx for unit_idx, unit in enumerate(units) for _ in unit]
    alignments, row_vectors = _group_nto1(
        raw_alignment,
        en_sentences,
        es_sentences,
        en_embeddings,
        model,
        es_para_indices=es_para_indices,
        es_vectors=es_embeddings[unit_of],
    )

    return _absorb_orphans(
        alignments, row_vectors, en_sentences, model, en_para_indices=en_para_indices
    )


def _group_nto1(
    raw_alignment: list[tuple[int, int, float]],
    en_sentences: list[str],
    es_sentences: list[str],
    en_embeddings: np.ndarray,
    model,
    es_para_indices: list[int] | None = None,
    es_vectors: np.ndarray | None = None,
) -> tuple[list[dict], list]:
    """
    Collapse consecutive alignment rows that share the same en_idx into a
    single output row. This happens naturally when Spanish renders one
    English quotation as two or more sentences (em-dash dialogue).

    For merged groups, recompute similarity on the concatenated Spanish text
    against the single English sentence. This removes the per-fragment
    scoring drag that shows up as low-confidence rows in the dashboard even
    when the translation is correct.

    Output rows expose an extended schema: `es_idx` remains the first
    Spanish index in the group (scalar, for correction-endpoint and DOM
    compatibility), with a new `es_indices` list when the group spans
    multiple sentences. `es` holds the joined text; `es_sentences` holds
    the original per-sentence texts for reader UIs that want to re-split
    them.

    When es_para_indices is provided, sentences from different paragraphs
    are never merged even if they map to the same en_idx.

    Returns the rows and, parallel to them, the embedding each row was scored
    with (``es_vectors`` holds one per Spanish sentence; a merged group uses
    the vector of its joined text), so _absorb_orphans need not encode the
    Spanish again.
    """
    if not raw_alignment:
        return [], []

    # Partition consecutive same-en_idx rows into groups.
    # Break a group when the next row crosses a paragraph boundary.
    groups: list[list[tuple[int, int, float]]] = []
    for row in raw_alignment:
        can_extend = (
            bool(groups)
            and groups[-1][-1][1] == row[1]
            and (
                es_para_indices is None
                or es_para_indices[row[0]] == es_para_indices[groups[-1][-1][0]]
            )
        )
        if can_extend:
            groups[-1].append(row)
        else:
            groups.append([row])

    # Recompute similarity for multi-row groups using a single batched encode
    merged_texts: list[str] = []
    merged_group_idx: list[int] = []
    for gi, grp in enumerate(groups):
        if len(grp) > 1:
            joined = " ".join(es_sentences[r[0]] for r in grp)
            merged_texts.append(_normalize_for_embedding(joined))
            merged_group_idx.append(gi)

    merged_sims: dict[int, float] = {}
    merged_vectors: dict[int, np.ndarray] = {}
    if merged_texts:
        merged_embeds = model.encode(merged_texts, normalize_embeddings=True)
        for local_i, gi in enumerate(merged_group_idx):
            en_idx = groups[gi][0][1]
            sim = float(np.dot(merged_embeds[local_i], en_embeddings[en_idx]))
            merged_sims[gi] = sim
            merged_vectors[gi] = merged_embeds[local_i]

    alignments: list[dict] = []
    row_vectors: list = []
    for gi, grp in enumerate(groups):
        es_indices = [int(r[0]) for r in grp]
        en_idx = int(grp[0][1])
        es_texts = [es_sentences[i] for i in es_indices]

        if len(grp) == 1:
            score = float(grp[0][2])
            record: dict = {
                "es_idx": es_indices[0],
                "en_idx": en_idx,
                "es": es_texts[0],
                "en": en_sentences[en_idx],
                "similarity": round(score, 3),
                "confidence": "high"
                if score > HIGH_CONFIDENCE_THRESHOLD
                else "low",
            }
        else:
            score = merged_sims[gi]
            record = {
                "es_idx": es_indices[0],
                "es_indices": es_indices,
                "en_idx": en_idx,
                "es": " ".join(es_texts),
                "es_sentences": es_texts,
                "en": en_sentences[en_idx],
                "similarity": round(score, 3),
                "confidence": "high"
                if score > HIGH_CONFIDENCE_THRESHOLD
                else "low",
            }

        if es_para_indices is not None:
            first_idx = es_indices[0]
            if first_idx > 0 and es_para_indices[first_idx] != es_para_indices[first_idx - 1]:
                record["para_start"] = True

        alignments.append(record)
        if len(grp) > 1:
            row_vectors.append(merged_vectors[gi])
        else:
            row_vectors.append(None if es_vectors is None else es_vectors[es_indices[0]])

    return alignments, row_vectors


def _substantive_chars(en_sentences: list[str], run: list[int]) -> tuple[int, list[int]]:
    """Character mass of a run that counts toward a coverage gap, and its members."""
    substantive = [
        i for i in run
        if len(en_sentences[i].strip()) >= MIN_SENTENCE_CHARS
        and _is_complete_sentence(en_sentences[i])
    ]
    return sum(len(en_sentences[i].strip()) for i in substantive), substantive


def _covered_en(alignments: list[dict]) -> set[int]:
    """Every source sentence index some row claims."""
    covered: set[int] = set()
    for a in alignments:
        covered.update(a.get("en_indices", [a["en_idx"]]))
    return covered


def _absorbable(en_sentences: list[str], run: list[int]) -> bool:
    """Whether an unclaimed run is small enough for a neighbouring row to take in."""
    if not run or len(run) > MAX_ABSORB_SENTENCES:
        return False
    if not any(ch.isalpha() for i in run for ch in en_sentences[i]):
        return False
    return _substantive_chars(en_sentences, run)[0] < MIN_GAP_CHARS


def _absorb_orphans(
    alignments: list[dict],
    row_vectors: list,
    en_sentences: list[str],
    model,
    en_para_indices: list[int] | None = None,
) -> list[dict]:
    """
    Let a row take in a short run of source sentences nothing else claimed.

    _monotonic_alignment gives each Spanish unit exactly one English sentence,
    so when Spanish says in one sentence what English says in two — or when a
    glued speech tag faces an English attribution written as its own sentence —
    the second English sentence is left unclaimed and the row shows only half
    its source. For each unclaimed run between two rows, the row before may
    extend forward over it or the row after may extend back, but only a row
    whose own source sentence sits in the same paragraph as the whole run. If
    both qualify, the one whose similarity against the joined English changes
    for the better takes it.

    The paragraph is the test, not the score. It is what tells a translator's
    merge, or a quotation and its attribution, from an untranslated caption or
    heading next door; similarity does not — on short dialogue it falls as
    often for a right attachment as for a wrong one (bench, 2026-10-07: of 40
    sampled absorptions 34 were right, and right and wrong ones fell alike).
    So nothing is absorbed without ``en_para_indices``.

    A row that absorbs gains ``en_indices`` (``en_idx`` stays its first), its
    ``en`` becomes the joined text, and its similarity is rescored. A row
    absorbs at most one run per pass. Runs longer than MAX_ABSORB_SENTENCES, or
    with enough substantive mass to be a reportable coverage gap, are never
    absorbed.
    """
    if not alignments or en_para_indices is None:
        return alignments

    def same_paragraph(run: list[int], en_idx: int) -> bool:
        return all(en_para_indices[i] == en_para_indices[en_idx] for i in run)

    # Per run, the (row index, first en, last en) spans that could take it.
    runs: list[list[tuple[int, int, int]]] = []
    prev_last = -1
    for r, row in enumerate(alignments):
        run = list(range(prev_last + 1, row["en_idx"]))
        if _absorbable(en_sentences, run):
            options = []
            if same_paragraph(run, row["en_idx"]):
                options.append((r, run[0], row["en_idx"]))
            if r > 0 and same_paragraph(run, alignments[r - 1]["en_idx"]):
                options.append((r - 1, alignments[r - 1]["en_idx"], run[-1]))
            runs.append(options)
        prev_last = max(prev_last, row["en_idx"])
    tail = list(range(prev_last + 1, len(en_sentences)))
    if _absorbable(en_sentences, tail) and same_paragraph(tail, alignments[-1]["en_idx"]):
        runs.append([(len(alignments) - 1, alignments[-1]["en_idx"], tail[-1])])

    runs = [[c for c in options if row_vectors[c[0]] is not None] for options in runs]
    flat = [c for options in runs for c in options]
    if not flat:
        return alignments

    texts = [
        _normalize_for_embedding(" ".join(en_sentences[first : last + 1]))
        for _, first, last in flat
    ]
    vectors = model.encode(texts, normalize_embeddings=True)
    scored = {c: float(np.dot(row_vectors[c[0]], vectors[k])) for k, c in enumerate(flat)}

    taken: set[int] = set()
    for options in runs:
        best = None
        for c in options:
            if c[0] in taken:
                continue
            gain = scored[c] - alignments[c[0]]["similarity"]
            if best is None or gain > best[0]:
                best = (gain, c)
        if best is None:
            continue
        r, first, last = best[1]
        taken.add(r)
        row = alignments[r]
        score = scored[best[1]]
        row["en_idx"] = first
        row["en_indices"] = list(range(first, last + 1))
        row["en"] = " ".join(en_sentences[first : last + 1])
        row["similarity"] = round(score, 3)
        row["confidence"] = "high" if score > HIGH_CONFIDENCE_THRESHOLD else "low"

    return alignments


def _is_complete_sentence(text: str) -> bool:
    """Whether a split record ends like a real sentence rather than a wrapped line."""
    stripped = text.strip().rstrip(SENTENCE_CLOSERS)
    return bool(stripped) and stripped[-1] in SENTENCE_TERMINALS


def _coverage_gaps(
    en_sentences: list[str],
    alignments: list[dict],
) -> list[dict]:
    """
    Find runs of source sentences that no target sentence claims.

    _monotonic_alignment maps every target sentence to exactly one source
    sentence, but lets source sentences be *skipped*. So when a translator
    silently drops a paragraph, the prose it should have produced is simply
    absent and the source sentences it covered are never referenced by any
    output row. That is invisible in every other metric — a dropped paragraph
    leaves the character ratio, the sentence counts, the paragraph counts and
    high_confidence_pct all looking normal — but it is exactly a run of missing
    en_idx values here.

    Runs are reported only when their substantive character mass clears
    MIN_GAP_CHARS. Below that a run is nearly always a 1-ES:N-EN merge, which
    the DP cannot represent (each ES sentence gets one EN sentence, so when
    Spanish packs two English sentences into one, the second goes unclaimed
    even though it *was* translated).

    Mass counts only records that are long enough (MIN_SENTENCE_CHARS) *and* end
    like a sentence, because a dropped paragraph is made of whole sentences. That
    second condition is what keeps hard-wrapped sources from reading as omissions:
    their line fragments never terminate, so a merged run of them contributes
    nothing. The known cost is unpunctuated verse — a dropped poem whose lines
    carry no terminal punctuation would not be reported.

    Returns one record per reported run:
        {
            "position": "head" | "interior" | "tail" | "full",
            "en_start": int,   # inclusive, chunk-local
            "en_end": int,     # inclusive, chunk-local
            "sentences": int,  # span length, including junk records
            "chars": int,      # substantive mass that cleared the threshold
            "preview": str,
        }

    ``position`` is relative to the chunk, so a "tail" gap on a chunk that is
    not the last in its chapter sits precisely on a chunk seam — the highest-
    signal case, and the one the Little Duke regression was.
    """
    if not en_sentences:
        return []

    covered = _covered_en(alignments)
    last_idx = len(en_sentences) - 1
    gaps: list[dict] = []

    def flush(run: list[int]) -> None:
        if not run:
            return
        chars, substantive = _substantive_chars(en_sentences, run)
        if chars < MIN_GAP_CHARS:
            return
        if run[0] == 0 and run[-1] == last_idx:
            # Entire chunk unclaimed (empty / fully dropped translation).
            position = "full"
        elif run[0] == 0:
            position = "head"
        elif run[-1] == last_idx:
            position = "tail"
        else:
            position = "interior"
        preview = en_sentences[substantive[0]].strip()
        gaps.append({
            "position": position,
            "en_start": run[0],
            "en_end": run[-1],
            "sentences": len(run),
            "chars": chars,
            "preview": preview[:100] + ("…" if len(preview) > 100 else ""),
        })

    run: list[int] = []
    for i in range(len(en_sentences)):
        if i in covered:
            flush(run)
            run = []
        else:
            run.append(i)
    flush(run)

    return gaps


def _coverage_summary(en_count: int, covered_count: int, gaps: list[dict]) -> dict:
    """Roll a gap list up into the summary block callers badge and warn on."""
    return {
        "en_count": en_count,
        "en_aligned": covered_count,
        "gap_count": len(gaps),
        "en_orphan_chars": sum(g["chars"] for g in gaps),
        "max_gap_chars": max((g["chars"] for g in gaps), default=0),
    }


def align_texts(
    source_text: str,
    translated_text: str,
    source_lang: str = "en",
    target_lang: str = "es",
    model=None,
) -> dict:
    """
    Split and align one source/translation pair.

    The core of :func:`align_chunk`, separated from the chunk file so the same
    path can be measured over cached embeddings. Returns the sentence lists
    alongside the rows and gaps:
        {"en_sentences": [...], "es_sentences": [...], "alignments": [...], "gaps": [...]}
    """
    en_sentences, en_para_indices = _split_sentences_with_para_indices(
        source_text, source_lang, split_quotes=True
    )
    es_sentences, es_para_indices = _split_sentences_with_para_indices(translated_text, target_lang)

    if model is None:
        model = _get_model()

    alignments = align_sentences(
        en_sentences,
        es_sentences,
        model,
        es_para_indices=es_para_indices,
        en_para_indices=en_para_indices,
    )

    return {
        "en_sentences": en_sentences,
        "es_sentences": es_sentences,
        "alignments": alignments,
        "gaps": _coverage_gaps(en_sentences, alignments),
    }


def align_chunk(
    chunk_path: str,
    source_lang: str = "en",
    target_lang: str = "es",
    model=None,
) -> dict:
    """
    Run end-to-end sentence alignment on a chunk JSON file.

    Returns:
        {
            "chapter_id": str,
            "chunk_id": str,
            "project_id": str,
            "en_count": int,
            "es_count": int,
            "high_confidence_pct": float,
            "avg_similarity": float,
            "coverage": {...},
            "gaps": [...],
            "alignments": [...]
        }

    ``gaps`` / ``coverage`` report source runs the translation never covered —
    see :func:`_coverage_gaps`. Indices are chunk-local.
    """
    path = Path(chunk_path)
    with open(path, encoding="utf-8") as f:
        chunk = json.load(f)

    source_text = chunk.get("source_text", "")
    translated_text = chunk.get("translated_text", "")

    if not source_text or not translated_text:
        raise ValueError(f"Missing source or translated text in {chunk_path}")

    aligned = align_texts(source_text, translated_text, source_lang, target_lang, model)
    en_sentences, es_sentences = aligned["en_sentences"], aligned["es_sentences"]
    alignments, gaps = aligned["alignments"], aligned["gaps"]

    high_conf_sentences = sum(
        len(a.get("es_indices", [a["es_idx"]]))
        for a in alignments
        if a["confidence"] == "high"
    )
    similarities = [a["similarity"] for a in alignments]

    return {
        "chapter_id": chunk.get("chapter_id", "unknown"),
        "chunk_id": chunk.get("id", path.stem),
        "en_count": len(en_sentences),
        "es_count": len(es_sentences),
        "high_confidence_pct": round(
            high_conf_sentences / len(es_sentences) * 100, 1
        )
        if es_sentences
        else 0,
        "avg_similarity": round(float(np.mean(similarities)), 3)
        if similarities
        else 0,
        "coverage": _coverage_summary(
            len(en_sentences), len(_covered_en(alignments)), gaps
        ),
        "gaps": gaps,
        "alignments": alignments,
    }


def align_chapter_chunks(
    chunk_paths: list[str],
    project_id: str,
    chapter_id: str,
    source_lang: str = "en",
    target_lang: str = "es",
    output_path: Optional[str] = None,
) -> dict:
    """
    Align all chunks for a chapter and produce a single chapter-level
    alignment file suitable for the reader UI.

    Loads the model once and reuses across chunks. Gap ``en_start``/``en_end``
    values are offset to chapter-global indices (same treatment as alignment
    rows); ``position`` stays chunk-relative. Return dict also includes
    ``coverage`` and ``gaps`` — see :func:`_coverage_gaps`.
    """
    model = _get_model()

    all_alignments = []
    all_gaps: list[dict] = []
    total_en = 0
    total_es = 0
    total_covered = 0

    for chunk_idx, chunk_path in enumerate(sorted(chunk_paths)):
        result = align_chunk(
            chunk_path,
            source_lang=source_lang,
            target_lang=target_lang,
            model=model,
        )
        # The chunker only splits on paragraph boundaries, so the first
        # sentence of every chunk after the first is itself a paragraph
        # start. align_chunk can't see chapter context — it only flags
        # para_start when the sentence's previous sentence lives in a
        # different paragraph within the same chunk — so we mark the
        # cross-chunk boundary here.
        if chunk_idx > 0 and result["alignments"]:
            result["alignments"][0]["para_start"] = True

        # Offset indices by cumulative counts
        for a in result["alignments"]:
            a["es_idx"] += total_es
            a["en_idx"] += total_en
            if "es_indices" in a:
                a["es_indices"] = [i + total_es for i in a["es_indices"]]
            if "en_indices" in a:
                a["en_indices"] = [i + total_en for i in a["en_indices"]]
            a["chunk_id"] = result["chunk_id"]

        # Same offset treatment for coverage gaps. `position` stays chunk-relative
        # on purpose: a "tail" gap on a non-final chunk is a chunk-seam drop, which
        # is the signal worth acting on.
        for g in result["gaps"]:
            g["en_start"] += total_en
            g["en_end"] += total_en
            g["chunk_id"] = result["chunk_id"]

        all_alignments.extend(result["alignments"])
        all_gaps.extend(result["gaps"])
        total_en += result["en_count"]
        total_es += result["es_count"]
        total_covered += result["coverage"]["en_aligned"]

    high_conf_sentences = sum(
        len(a.get("es_indices", [a["es_idx"]]))
        for a in all_alignments
        if a["confidence"] == "high"
    )
    similarities = [a["similarity"] for a in all_alignments]

    chapter_alignment = {
        "chapter_id": chapter_id,
        "project_id": project_id,
        "en_count": total_en,
        "es_count": total_es,
        "high_confidence_pct": round(high_conf_sentences / total_es * 100, 1)
        if total_es
        else 0,
        "avg_similarity": round(float(np.mean(similarities)), 3)
        if similarities
        else 0,
        "coverage": _coverage_summary(total_en, total_covered, all_gaps),
        "gaps": all_gaps,
        "alignments": all_alignments,
    }

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(chapter_alignment, f, ensure_ascii=False, indent=2)

    return chapter_alignment
