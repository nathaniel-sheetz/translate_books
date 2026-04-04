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

# Lazy-loaded to avoid slow import when not needed
_model = None

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
MAX_SENTENCE_WORDS = 50
HIGH_CONFIDENCE_THRESHOLD = 0.5
SKIP_PENALTY = 0.05


def _get_model():
    """Lazy-load the sentence-transformers model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _split_long_sentence(text: str) -> list[str]:
    """
    Split a long sentence on sentence-ending punctuation followed by
    space + uppercase or quote character. Fixes pysbd's tendency to
    treat entire quoted dialogue passages as single sentences.
    """
    parts = re.split(
        r"(?<=[.!?])\s+(?=[A-Z\u00BF\u00A1'\"\u201C\u2018\(\[])",
        text,
    )
    return [p.strip() for p in parts if p.strip()]


def split_sentences(text: str, language: str) -> list[str]:
    """
    Split text into sentences using pysbd, then post-split any
    sentences longer than MAX_SENTENCE_WORDS.
    """
    segmenter = pysbd.Segmenter(language=language, clean=False)
    raw_sentences = segmenter.segment(text)

    result = []
    for sent in raw_sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(sent.split()) > MAX_SENTENCE_WORDS:
            sub_sents = _split_long_sentence(sent)
            if len(sub_sents) > 1:
                result.extend(sub_sents)
            else:
                result.append(sent)
        else:
            result.append(sent)

    return result


def _monotonic_alignment(
    similarity: np.ndarray,
) -> list[tuple[int, int, float]]:
    """
    Find the best monotonically non-decreasing alignment between
    target sentences (rows) and source sentences (columns) using DP.

    Each target sentence maps to exactly one source sentence.
    Source sentences can be skipped or shared (many-to-one).
    """
    n_tgt, n_src = similarity.shape

    dp = np.full((n_tgt, n_src), -np.inf)
    backtrack = np.full((n_tgt, n_src), -1, dtype=int)

    # Base case
    for j in range(n_src):
        dp[0][j] = similarity[0][j]

    # Fill
    for i in range(1, n_tgt):
        for j in range(n_src):
            sim = similarity[i][j]
            best_prev = -np.inf
            best_k = -1
            for k in range(j + 1):
                score = dp[i - 1][k]
                skipped = j - k - 1
                if skipped > 0:
                    score -= skipped * SKIP_PENALTY
                if score > best_prev:
                    best_prev = score
                    best_k = k
            dp[i][j] = best_prev + sim
            backtrack[i][j] = best_k

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
    """
    if not en_sentences or not es_sentences:
        return []

    if model is None:
        model = _get_model()

    en_embeddings = model.encode(en_sentences, normalize_embeddings=True)
    es_embeddings = model.encode(es_sentences, normalize_embeddings=True)

    similarity = np.dot(es_embeddings, en_embeddings.T)
    raw_alignment = _monotonic_alignment(similarity)

    alignments = []
    for es_idx, en_idx, score in raw_alignment:
        alignments.append(
            {
                "es_idx": int(es_idx),
                "en_idx": int(en_idx),
                "es": es_sentences[es_idx],
                "en": en_sentences[en_idx],
                "similarity": round(score, 3),
                "confidence": "high"
                if score > HIGH_CONFIDENCE_THRESHOLD
                else "low",
            }
        )

    return alignments


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
            "alignments": [...]
        }
    """
    path = Path(chunk_path)
    with open(path, encoding="utf-8") as f:
        chunk = json.load(f)

    source_text = chunk.get("source_text", "")
    translated_text = chunk.get("translated_text", "")

    if not source_text or not translated_text:
        raise ValueError(f"Missing source or translated text in {chunk_path}")

    en_sentences = split_sentences(source_text, source_lang)
    es_sentences = split_sentences(translated_text, target_lang)

    if model is None:
        model = _get_model()

    alignments = align_sentences(en_sentences, es_sentences, model)

    high_conf = sum(1 for a in alignments if a["confidence"] == "high")
    similarities = [a["similarity"] for a in alignments]

    return {
        "chapter_id": chunk.get("chapter_id", "unknown"),
        "chunk_id": chunk.get("id", path.stem),
        "en_count": len(en_sentences),
        "es_count": len(es_sentences),
        "high_confidence_pct": round(
            high_conf / len(es_sentences) * 100, 1
        )
        if es_sentences
        else 0,
        "avg_similarity": round(float(np.mean(similarities)), 3)
        if similarities
        else 0,
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
    total_en = 0
    total_es = 0

    for chunk_path in sorted(chunk_paths):
        result = align_chunk(
            chunk_path,
            source_lang=source_lang,
            target_lang=target_lang,
            model=model,
        )
        # Offset indices by cumulative counts
        for a in result["alignments"]:
            a["es_idx"] += total_es
            a["en_idx"] += total_en
            a["chunk_id"] = result["chunk_id"]

        all_alignments.extend(result["alignments"])
        total_en += result["en_count"]
        total_es += result["es_count"]

    high_conf = sum(1 for a in all_alignments if a["confidence"] == "high")
    similarities = [a["similarity"] for a in all_alignments]

    chapter_alignment = {
        "chapter_id": chapter_id,
        "project_id": project_id,
        "en_count": total_en,
        "es_count": total_es,
        "high_confidence_pct": round(high_conf / total_es * 100, 1)
        if total_es
        else 0,
        "avg_similarity": round(float(np.mean(similarities)), 3)
        if similarities
        else 0,
        "alignments": all_alignments,
    }

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(chapter_alignment, f, ensure_ascii=False, indent=2)

    return chapter_alignment
