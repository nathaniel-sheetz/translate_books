"""
Sentence-level alignment between source and translated text.

Uses pysbd for sentence boundary detection, a post-split step for
oversized sentences (common in English literary dialogue), and
sentence-transformers embeddings with monotonic dynamic programming
to find the best alignment.

Validated at 95.5% high-confidence alignment across dialogue-heavy
chapters (fabre2 ch06/24/25, lang-faerie ch01).
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

# Coverage-gap reporting (see _coverage_gaps). Calibrated over 505 translated
# chunks across 18 books: substantive orphan-run mass is 0 for 96.6% of chunks,
# under 300 chars for another 1.6%, and there is an empty band between 300 and
# 499 before the real omissions start. 300 flagged 10 chunks, all of them
# genuine drops, and zero false positives.
MIN_GAP_CHARS = 300
# Sentence splitting emits standalone junk records for Gutenberg rules ("---"),
# stray quote marks and verse punctuation. They are never "translated", so they
# must not contribute to a gap's mass.
MIN_SENTENCE_CHARS = 25


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


def _split_long_sentence(text: str) -> list[str]:
    """
    Split a long sentence on sentence-ending punctuation (optionally
    followed by a closing quote/bracket) and whitespace, before an
    uppercase letter or opening quote/bracket. Fixes pysbd's tendency
    to treat entire quoted dialogue passages as single sentences,
    including the common case `."  "Next…` where the closing quote
    sits between the period and the whitespace.
    """
    parts = _SPLIT_LONG_RE.split(text)
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
            sub_sents = _split_long_sentence(sent)
            if len(sub_sents) > 1:
                result.extend(sub_sents)
            else:
                result.append(sent)
        else:
            result.append(sent)

    return result


def _split_sentences_with_para_indices(text: str, language: str) -> tuple[list[str], list[int]]:
    """
    Split multi-paragraph text into sentences, tracking which paragraph
    each sentence came from.

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
            sentences.extend(para_sents)
            para_indices.extend([para_idx] * len(para_sents))
    return sentences, para_indices


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
    """
    if not en_sentences or not es_sentences:
        return []

    if model is None:
        model = _get_model()

    en_for_embed = [_normalize_for_embedding(s) for s in en_sentences]
    es_for_embed = [_normalize_for_embedding(s) for s in es_sentences]

    en_embeddings = model.encode(en_for_embed, normalize_embeddings=True)
    es_embeddings = model.encode(es_for_embed, normalize_embeddings=True)

    similarity = np.dot(es_embeddings, en_embeddings.T)
    raw_alignment = _monotonic_alignment(similarity)

    alignments = _group_nto1(
        raw_alignment,
        en_sentences,
        es_sentences,
        en_embeddings,
        model,
        es_para_indices=es_para_indices,
    )

    return alignments


def _group_nto1(
    raw_alignment: list[tuple[int, int, float]],
    en_sentences: list[str],
    es_sentences: list[str],
    en_embeddings: np.ndarray,
    model,
    es_para_indices: list[int] | None = None,
) -> list[dict]:
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
    """
    if not raw_alignment:
        return []

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
    if merged_texts:
        merged_embeds = model.encode(merged_texts, normalize_embeddings=True)
        for local_i, gi in enumerate(merged_group_idx):
            en_idx = groups[gi][0][1]
            sim = float(np.dot(merged_embeds[local_i], en_embeddings[en_idx]))
            merged_sims[gi] = sim

    alignments: list[dict] = []
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

    return alignments


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

    Returns one record per reported run:
        {
            "position": "head" | "interior" | "tail",
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

    covered = {a["en_idx"] for a in alignments}
    last_idx = len(en_sentences) - 1
    gaps: list[dict] = []

    def flush(run: list[int]) -> None:
        if not run:
            return
        substantive = [
            i for i in run if len(en_sentences[i].strip()) >= MIN_SENTENCE_CHARS
        ]
        chars = sum(len(en_sentences[i].strip()) for i in substantive)
        if chars < MIN_GAP_CHARS:
            return
        if run[0] == 0:
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

    en_sentences, _ = _split_sentences_with_para_indices(source_text, source_lang)
    es_sentences, es_para_indices = _split_sentences_with_para_indices(translated_text, target_lang)

    if model is None:
        model = _get_model()

    alignments = align_sentences(en_sentences, es_sentences, model, es_para_indices=es_para_indices)

    high_conf_sentences = sum(
        len(a.get("es_indices", [a["es_idx"]]))
        for a in alignments
        if a["confidence"] == "high"
    )
    similarities = [a["similarity"] for a in alignments]

    gaps = _coverage_gaps(en_sentences, alignments)

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
            len(en_sentences), len({a["en_idx"] for a in alignments}), gaps
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

    Loads the model once and reuses across chunks.
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
