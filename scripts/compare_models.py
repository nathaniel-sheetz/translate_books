#!/usr/bin/env python3
"""
Compare translation models using the LLM-judge evaluator.

Pipeline:
  source chapter (txt)
      |
      v
  [chunker] ─────────────────────────────────── existing
      |
      v
  for each model in --models:
     [translate_chapter_with_model] → chunks   (Anthropic batch API)
      |
      v
  for each chunk × model:
     [run_all_evaluators] → coded EvalResults   (grammar, glossary, etc.)
      |
      v
  pairwise loop (round-robin model pairs):
     for each chunk pair (a, b):
        swap A/B order randomly, record position_a_model
        [format_signals_for_judge] → str
        [judge_pairwise] → PairwiseVerdict
      |
      v
  [write CSV row] + [append raw JSONL]
      |
      v
  [bootstrap CI on win rates] → summary (with bias warning if applicable)

Usage:
    python scripts/compare_models.py \\
        --source projects/fabre2/chapters/chapter_01.txt \\
        --models sonnet,haiku \\
        --judge claude-sonnet-4-6 \\
        --project projects/fabre2

    python scripts/compare_models.py \\
        --source projects/fabre2/chapters/chapter_01.txt \\
        --models claude-sonnet-4-6,claude-haiku-4-5-20251001 \\
        --judge claude-sonnet-4-6 \\
        --style projects/fabre2/style.json \\
        --cost-limit 10 --confirm
"""

import argparse
import csv
import hashlib
import itertools
import json
import logging
import random
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api_translator import (
    APIError,
    estimate_cost,
    get_default_provider,
    get_model_pricing,
    translate_chapter_with_model,
)
from src.chunker import chunk_chapter
from src.evaluators import run_all_evaluators
from src.judge import (
    JudgeParseError,
    format_signals_for_judge,
    get_prompt_version,
    judge_pairwise,
)
from src.models import (
    Chunk,
    ChunkingConfig,
    EvalResult,
    EvaluationConfig,
    PairwiseVerdict,
)
from src.utils.file_io import load_glossary, load_style_guide

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model alias resolution
# ---------------------------------------------------------------------------

_MODEL_ALIASES: dict[str, str] = {
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "opus": "claude-opus-4-0-20250514",
    "gpt4o": "gpt-4o",
    "gpt4o-mini": "gpt-4o-mini",
}


def _resolve_model(alias: str) -> str:
    """Resolve a short alias to a full model ID, or pass through."""
    return _MODEL_ALIASES.get(alias.lower(), alias)


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Cost estimation (C3)
# ---------------------------------------------------------------------------

def _estimate_total_cost(
    chunks: list[Chunk],
    models: list[str],
    judge_model: str,
    provider: str,
    voice_tokens: int,
    n_coded_signal_tokens: int = 100,
    rubric_tokens: int = 300,
) -> dict:
    """Estimate total cost for translations + judge calls.

    Returns dict with ``translation_cost``, ``judge_cost``, ``total_cost``,
    and ``breakdown`` string.
    """
    # Translation cost per model
    total_translation = 0.0
    for model in models:
        est = estimate_cost(chunks, provider, model, batch_mode=True)
        total_translation += est["cost_usd"]

    # Judge cost: pairwise calls = C(n,2) * n_chunks * 2 swaps
    n_pairs = len(models) * (len(models) - 1) // 2
    n_chunks = len(chunks)
    n_judge_calls = n_pairs * n_chunks  # one call per pair per chunk

    avg_source_tokens = sum(len(c.source_text) // 4 for c in chunks) / max(n_chunks, 1)
    avg_trans_tokens = avg_source_tokens  # rough estimate
    prompt_tokens_per_call = (
        avg_source_tokens
        + avg_trans_tokens * 2  # pairwise: two translations
        + voice_tokens
        + n_coded_signal_tokens
        + rubric_tokens
    )
    judge_output_tokens = 300  # typical JSON response
    judge_pricing = get_model_pricing(provider, judge_model)
    judge_input_cost = (prompt_tokens_per_call * n_judge_calls / 1_000_000) * judge_pricing["input"]
    judge_output_cost = (judge_output_tokens * n_judge_calls / 1_000_000) * judge_pricing["output"]
    total_judge = judge_input_cost + judge_output_cost

    total = total_translation + total_judge

    breakdown = (
        f"Estimated cost:\n"
        f"  Translation: ${total_translation:.4f} ({len(models)} models × {n_chunks} chunks, batch 50% discount)\n"
        f"  Judge:       ${total_judge:.4f} ({n_pairs} pairs × {n_chunks} chunks × ~{int(prompt_tokens_per_call)} tokens avg)\n"
        f"  Total:       ${total:.4f}"
    )

    prompt_log_size_kb = n_judge_calls * 4  # ~4 KB each
    breakdown += f"\n  Estimated prompt log size: ~{prompt_log_size_kb / 1024:.1f} MB ({n_judge_calls} judge calls × ~4 KB each)"

    return {
        "translation_cost": total_translation,
        "judge_cost": total_judge,
        "total_cost": total,
        "n_judge_calls": n_judge_calls,
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# Bootstrap CI (C2)
# ---------------------------------------------------------------------------

def _bootstrap_ci(
    wins: int,
    total: int,
    n_resamples: int = 10_000,
    ci: float = 0.95,
) -> tuple[float, float, float]:
    """Compute bootstrap CI on a win rate.

    Returns (win_rate, ci_low, ci_high).
    """
    if total == 0:
        return 0.0, 0.0, 0.0

    data = np.array([1] * wins + [0] * (total - wins))
    rng = np.random.default_rng(42)
    boot_rates = np.array([
        rng.choice(data, size=total, replace=True).mean()
        for _ in range(n_resamples)
    ])
    alpha = (1 - ci) / 2
    lo = float(np.quantile(boot_rates, alpha))
    hi = float(np.quantile(boot_rates, 1 - alpha))
    return wins / total, lo, hi


def _effective_n(chunks: list[Chunk]) -> int:
    """Estimate effective independent N accounting for chunker overlap."""
    if len(chunks) <= 1:
        return len(chunks)
    total_overlap_chars = sum(
        c.metadata.overlap_start + c.metadata.overlap_end for c in chunks
    )
    total_chars = sum(len(c.source_text) for c in chunks)
    if total_chars == 0:
        return len(chunks)
    overlap_ratio = total_overlap_chars / total_chars
    effective = int(len(chunks) * (1 - overlap_ratio / 2))
    return max(1, effective)


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------

_PAIRWISE_FIELDS = [
    "run_id", "project_id", "chapter_id", "chunk_id", "source_hash",
    "position_a_model", "position_b_model",
    "translation_a_hash", "translation_b_hash",
    "judge_model", "judge_prompt_version",
    "fluency_winner", "fidelity_winner", "regional_winner",
    "voice_winner", "overall_winner",
    "word_count_a", "word_count_b",
    "status", "judge_rationale_short",
]


def _write_csv_header(csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_PAIRWISE_FIELDS)
        writer.writeheader()


def _append_csv_row(csv_path: Path, row: dict) -> None:
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_PAIRWISE_FIELDS)
        writer.writerow(row)


# ---------------------------------------------------------------------------
# JSONL writer (raw judge output + run header)
# ---------------------------------------------------------------------------

def _write_jsonl_header(
    jsonl_path: Path,
    run_id: str,
    judge_model: str,
    judge_prompt_version: str,
    git_commit: str,
) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "type": "run_header",
        "run_id": run_id,
        "judge_model": judge_model,
        "judge_temperature": 0.0,
        "judge_prompt_version": judge_prompt_version,
        "git_commit": git_commit,
        "judge_max_tokens": 4096,
        "started_at": datetime.now().isoformat(),
    }
    with jsonl_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")


def _append_jsonl(jsonl_path: Path, record: dict) -> None:
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def run_comparison(args: argparse.Namespace) -> None:
    """Execute the full comparison pipeline."""
    run_id = str(uuid.uuid4())
    source_path = Path(args.source)
    project_path = Path(args.project)
    project_id = project_path.name

    # Resolve models
    models = [_resolve_model(m.strip()) for m in args.models.split(",")]
    judge_model = _resolve_model(args.judge)
    provider = args.provider or get_default_provider()

    if len(models) < 2:
        print("ERROR: Need at least 2 models to compare.")
        sys.exit(1)

    # Load chapter and chunk
    print(f"\n{'='*60}")
    print(f"  LLM Translation Model Comparison")
    print(f"  Run ID: {run_id}")
    print(f"{'='*60}\n")

    chapter_text = source_path.read_text(encoding="utf-8")
    chapter_id = source_path.stem
    config = ChunkingConfig()
    chunks = chunk_chapter(chapter_text, config, chapter_id=chapter_id)
    print(f"Source: {source_path} → {len(chunks)} chunks")

    eff_n = _effective_n(chunks)
    print(f"Effective independent N: ~{eff_n} (overlap-adjusted)")
    if eff_n < 50:
        print(
            "WARNING: N_effective < 50; \"winner\" claims are noise-prone. "
            "Single-chapter runs are exploratory only."
        )

    # Load optional resources
    style_json_path: Optional[Path] = None
    voice_tokens = 0
    if args.style:
        style_json_path = Path(args.style)
        if style_json_path.exists():
            style_data = json.loads(style_json_path.read_text(encoding="utf-8"))
            voice_tokens = len(style_data.get("content", "")) // 4
            print(f"Style guide: {style_json_path} (~{voice_tokens} tokens)")
        else:
            print(f"WARNING: Style guide not found: {style_json_path}")
            style_json_path = None
    else:
        # Auto-detect style.json in project dir
        auto_style = project_path / "style.json"
        if auto_style.exists():
            style_json_path = auto_style
            style_data = json.loads(auto_style.read_text(encoding="utf-8"))
            voice_tokens = len(style_data.get("content", "")) // 4
            print(f"Style guide (auto): {auto_style} (~{voice_tokens} tokens)")

    glossary = None
    glossary_path = project_path / "glossary.json"
    if glossary_path.exists():
        try:
            glossary = load_glossary(glossary_path)
            print(f"Glossary: {glossary_path}")
        except Exception as exc:
            print(f"WARNING: Could not load glossary: {exc}")

    style_guide = None
    if style_json_path and style_json_path.exists():
        try:
            style_guide = load_style_guide(style_json_path)
        except Exception:
            pass

    # Cost estimate (C3)
    cost_est = _estimate_total_cost(
        chunks, models, judge_model, provider, voice_tokens,
    )
    print(f"\n{cost_est['breakdown']}")

    cost_limit = args.cost_limit
    if cost_est["total_cost"] > cost_limit and not args.confirm:
        print(
            f"\n  Estimated cost ${cost_est['total_cost']:.4f} exceeds "
            f"limit ${cost_limit:.2f}. Pass --confirm to override."
        )
        sys.exit(1)

    # Setup output dirs (C8)
    output_base = Path(args.output) if args.output else Path("comparisons") / project_id / run_id
    output_base.mkdir(parents=True, exist_ok=True)
    csv_path = output_base / "pairwise.csv"
    jsonl_path = output_base / "raw.jsonl"
    prompts_dir = output_base / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    # Save style guide once per run
    if style_json_path and style_json_path.exists():
        import shutil
        shutil.copy2(style_json_path, output_base / "style_guide.txt")

    # Reproducibility metadata (C7)
    git_sha = _git_commit()
    prompt_version = get_prompt_version("judge_pairwise.txt")

    _write_csv_header(csv_path)
    _write_jsonl_header(jsonl_path, run_id, judge_model, prompt_version, git_sha)

    print(f"\nOutput: {output_base}")
    print(f"Models: {', '.join(models)}")
    print(f"Judge:  {judge_model}")
    print(f"Git:    {git_sha[:12]}")
    print()

    # --- Phase 1: Translate with each model ---
    print("Phase 1: Translating with each model...")
    translations: dict[str, list[Chunk]] = {}
    failed_models: list[str] = []

    for model in models:
        print(f"  Translating with {model}...", end=" ", flush=True)
        try:
            translated = translate_chapter_with_model(
                chunks, model, project_path,
                provider=provider,
                glossary=glossary,
                style_guide=style_guide,
                project_name=project_id,
            )
            translations[model] = translated
            print(f"✓ {len(translated)}/{len(chunks)} chunks")
        except APIError as exc:
            print(f"✗ FAILED: {exc}")
            failed_models.append(model)
            logger.error("Translation failed for model %s: %s", model, exc)

    # F3: skip failed models, abort if <2 remain
    surviving_models = [m for m in models if m not in failed_models]
    if len(surviving_models) < 2:
        print(f"\nERROR: Only {len(surviving_models)} model(s) succeeded. Need ≥2 for comparison.")
        if failed_models:
            print(f"Failed: {', '.join(failed_models)}")
        sys.exit(1)

    if failed_models:
        print(f"\nWARNING: Skipping failed models from comparison: {', '.join(failed_models)}")

    # Build chunk-id lookup for each model
    model_chunk_map: dict[str, dict[str, Chunk]] = {}
    for model in surviving_models:
        model_chunk_map[model] = {c.id: c for c in translations[model]}

    # --- Phase 2: Run coded evaluators per model ---
    print("\nPhase 2: Running coded evaluators...")
    eval_config = EvaluationConfig(enabled_evals=["length", "paragraph"])
    model_eval_results: dict[str, dict[str, list[EvalResult]]] = {}

    for model in surviving_models:
        model_eval_results[model] = {}
        for tc in translations[model]:
            results = run_all_evaluators(tc, eval_config, glossary=glossary)
            model_eval_results[model][tc.id] = results
    print("  ✓ Coded evaluators complete")

    # --- Phase 3: Pairwise judge comparisons ---
    print("\nPhase 3: Pairwise judge comparisons...")
    pairs = list(itertools.combinations(surviving_models, 2))
    print(f"  {len(pairs)} model pair(s) × {len(chunks)} chunks = {len(pairs) * len(chunks)} comparisons\n")

    # Accumulators for summary
    all_rows: list[dict] = []
    verdicts_by_pair: dict[tuple[str, str], list[dict]] = {p: [] for p in pairs}
    rationale_samples: list[str] = []
    position_a_wins = 0
    position_b_wins = 0
    total_comparisons = 0

    for model_a, model_b in pairs:
        print(f"  Comparing {model_a} vs {model_b}...")

        for chunk in chunks:
            chunk_id = chunk.id

            # Check both models have this chunk (C5)
            tc_a = model_chunk_map[model_a].get(chunk_id)
            tc_b = model_chunk_map[model_b].get(chunk_id)

            if tc_a is None or tc_b is None:
                # batch_omitted row
                row = {
                    "run_id": run_id,
                    "project_id": project_id,
                    "chapter_id": chapter_id,
                    "chunk_id": chunk_id,
                    "source_hash": _sha256(chunk.source_text),
                    "position_a_model": model_a,
                    "position_b_model": model_b,
                    "translation_a_hash": "",
                    "translation_b_hash": "",
                    "judge_model": judge_model,
                    "judge_prompt_version": prompt_version,
                    "fluency_winner": "",
                    "fidelity_winner": "",
                    "regional_winner": "",
                    "voice_winner": "",
                    "overall_winner": "",
                    "word_count_a": 0,
                    "word_count_b": 0,
                    "status": "batch_omitted",
                    "judge_rationale_short": f"Missing translation: {'A' if tc_a is None else 'B'}",
                }
                _append_csv_row(csv_path, row)
                all_rows.append(row)
                continue

            # F10: Randomize A/B order
            swap = random.random() < 0.5
            if swap:
                pos_a_model, pos_b_model = model_b, model_a
                trans_a, trans_b = tc_b, tc_a
                evals_a = model_eval_results[model_b].get(chunk_id, [])
                evals_b = model_eval_results[model_a].get(chunk_id, [])
            else:
                pos_a_model, pos_b_model = model_a, model_b
                trans_a, trans_b = tc_a, tc_b
                evals_a = model_eval_results[model_a].get(chunk_id, [])
                evals_b = model_eval_results[model_b].get(chunk_id, [])

            # Call judge
            try:
                verdict: PairwiseVerdict = judge_pairwise(
                    source_text=chunk.source_text,
                    translation_a=trans_a.translated_text,
                    translation_b=trans_b.translated_text,
                    style_json_path=style_json_path,
                    coded_eval_results_a=evals_a,
                    coded_eval_results_b=evals_b,
                    judge_provider=provider,
                    judge_model=judge_model,
                )
                status = "success"
                rationale_short = verdict.rationale[:200]

                # Un-swap verdict if we swapped (map A/B back to real models)
                if swap:
                    fluency_w = _unswap(verdict.fluency_winner)
                    fidelity_w = _unswap(verdict.fidelity_winner)
                    regional_w = _unswap(verdict.regional_winner)
                    voice_w = _unswap(verdict.voice_winner) if verdict.voice_winner != "N/A" else "N/A"
                    overall_w = _unswap(verdict.overall_winner)
                else:
                    fluency_w = verdict.fluency_winner
                    fidelity_w = verdict.fidelity_winner
                    regional_w = verdict.regional_winner
                    voice_w = verdict.voice_winner
                    overall_w = verdict.overall_winner

                # Track position bias
                total_comparisons += 1
                if verdict.overall_winner == "A":
                    position_a_wins += 1
                elif verdict.overall_winner == "B":
                    position_b_wins += 1

                # Collect rationale samples
                if len(rationale_samples) < 3:
                    rationale_samples.append(
                        f"  [{pos_a_model} vs {pos_b_model}, {chunk_id}]\n"
                        f"  {verdict.rationale[:300]}"
                    )

                # Append raw JSONL
                _append_jsonl(jsonl_path, {
                    "type": "verdict",
                    "run_id": run_id,
                    "chunk_id": chunk_id,
                    "position_a_model": pos_a_model,
                    "position_b_model": pos_b_model,
                    "swapped": swap,
                    "verdict": verdict.model_dump(mode="json"),
                })

            except (JudgeParseError, APIError) as exc:
                status = "judge_failed"
                rationale_short = str(exc)[:200]
                fluency_w = fidelity_w = regional_w = voice_w = overall_w = ""
                logger.error("Judge failed on %s: %s", chunk_id, exc)

            row = {
                "run_id": run_id,
                "project_id": project_id,
                "chapter_id": chapter_id,
                "chunk_id": chunk_id,
                "source_hash": _sha256(chunk.source_text),
                "position_a_model": pos_a_model,
                "position_b_model": pos_b_model,
                "translation_a_hash": _sha256(trans_a.translated_text or ""),
                "translation_b_hash": _sha256(trans_b.translated_text or ""),
                "judge_model": judge_model,
                "judge_prompt_version": prompt_version,
                "fluency_winner": fluency_w,
                "fidelity_winner": fidelity_w,
                "regional_winner": regional_w,
                "voice_winner": voice_w,
                "overall_winner": overall_w,
                "word_count_a": len((trans_a.translated_text or "").split()),
                "word_count_b": len((trans_b.translated_text or "").split()),
                "status": status,
                "judge_rationale_short": rationale_short,
            }
            _append_csv_row(csv_path, row)
            all_rows.append(row)
            verdicts_by_pair[(model_a, model_b)].append(row)

    # --- Phase 4: Summary ---
    print(f"\n{'='*60}")
    print("  COMPARISON SUMMARY")
    print(f"{'='*60}\n")

    success_rows = [r for r in all_rows if r["status"] == "success"]
    failed_rows = [r for r in all_rows if r["status"] != "success"]

    print(f"Total comparisons: {len(all_rows)} ({len(success_rows)} success, {len(failed_rows)} failed/omitted)")
    print(f"Effective N: ~{eff_n}")
    print()

    # Per-pair win rates with bootstrap CI
    for (ma, mb), rows in verdicts_by_pair.items():
        ok_rows = [r for r in rows if r["status"] == "success"]
        if not ok_rows:
            print(f"  {ma} vs {mb}: no successful comparisons")
            continue

        total = len(ok_rows)
        # "A" in the CSV is mapped back to the original model_a
        a_wins = sum(1 for r in ok_rows if r["overall_winner"] == "A")
        b_wins = sum(1 for r in ok_rows if r["overall_winner"] == "B")
        ties = sum(1 for r in ok_rows if r["overall_winner"] == "tie")

        rate_a, lo_a, hi_a = _bootstrap_ci(a_wins, total)
        rate_b, lo_b, hi_b = _bootstrap_ci(b_wins, total)

        print(f"  {ma} vs {mb}  ({total} comparisons):")
        print(f"    {ma} wins {a_wins}/{total} = {rate_a:.0%} (95% CI [{lo_a:.0%}, {hi_a:.0%}])")
        print(f"    {mb} wins {b_wins}/{total} = {rate_b:.0%} (95% CI [{lo_b:.0%}, {hi_b:.0%}])")
        print(f"    ties: {ties}")

        # Per-dimension breakdown
        for dim in ["fluency", "fidelity", "regional", "voice"]:
            dim_key = f"{dim}_winner"
            dim_a = sum(1 for r in ok_rows if r[dim_key] == "A")
            dim_b = sum(1 for r in ok_rows if r[dim_key] == "B")
            dim_t = sum(1 for r in ok_rows if r[dim_key] == "tie")
            if dim == "voice" and all(r[dim_key] in ("N/A", "") for r in ok_rows):
                continue
            print(f"      {dim:12s}:  {ma}={dim_a}  {mb}={dim_b}  tie={dim_t}")
        print()

    # Position bias warning
    if total_comparisons > 0:
        a_rate = position_a_wins / total_comparisons
        b_rate = position_b_wins / total_comparisons
        print(f"Position bias check: A-position wins {a_rate:.0%}, B-position wins {b_rate:.0%}")
        if a_rate > 0.70 or b_rate > 0.70:
            print("  ⚠️  WARNING: Position bias detected (>70% for one position).")
            print("  Judge may be favoring A/B position rather than content.")
        print()

    # Length-bucket breakdown (C6)
    if success_rows:
        print("Length-bucket breakdown:")
        word_counts = [(int(r["word_count_a"]) + int(r["word_count_b"])) // 2 for r in success_rows]
        buckets = {"short (<100w)": [], "medium (100-300w)": [], "long (>300w)": []}
        for r, wc in zip(success_rows, word_counts):
            if wc < 100:
                buckets["short (<100w)"].append(r)
            elif wc <= 300:
                buckets["medium (100-300w)"].append(r)
            else:
                buckets["long (>300w)"].append(r)

        for bname, brows in buckets.items():
            if not brows:
                continue
            a_w = sum(1 for r in brows if r["overall_winner"] == "A")
            b_w = sum(1 for r in brows if r["overall_winner"] == "B")
            t_w = sum(1 for r in brows if r["overall_winner"] == "tie")
            print(f"  {bname}: A={a_w} B={b_w} tie={t_w} (n={len(brows)})")
        print()

    # Cost summary
    print(f"Cost estimate: ${cost_est['total_cost']:.4f}")
    print()

    # Sample rationales
    if rationale_samples:
        print("Sample judge rationales:")
        for sample in rationale_samples:
            print(sample)
            print()

    # Minimum-N caveat
    print(
        "Min recommended N for any 'model A is better' claim: 50 effective chunks. "
        "Single-chapter runs are exploratory only."
    )

    print(f"\nCSV:   {csv_path}")
    print(f"JSONL: {jsonl_path}")
    print(f"Done.\n")


def _unswap(winner: str) -> str:
    """Un-swap A/B when the presentation order was reversed."""
    if winner == "A":
        return "B"
    if winner == "B":
        return "A"
    return winner  # tie


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare translation models using LLM-judge evaluation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", required=True,
        help="Path to source chapter text file (e.g. projects/fabre2/chapters/chapter_01.txt)",
    )
    parser.add_argument(
        "--models", required=True,
        help="Comma-separated model IDs or aliases (e.g. sonnet,haiku or claude-sonnet-4-6,claude-haiku-4-5-20251001)",
    )
    parser.add_argument(
        "--judge", default="claude-sonnet-4-6",
        help="Model to use as the judge (default: claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--project", required=True,
        help="Path to the project directory (e.g. projects/fabre2)",
    )
    parser.add_argument(
        "--provider", default=None,
        help="LLM provider (default: from llm_config.json)",
    )
    parser.add_argument(
        "--style", default=None,
        help="Path to style.json for voice context (auto-detected from project if omitted)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output directory (default: comparisons/<project_id>/<run_id>/)",
    )
    parser.add_argument(
        "--cost-limit", type=float, default=5.0,
        help="Maximum estimated cost in USD before requiring --confirm (default: $5)",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Confirm and proceed even if cost estimate exceeds --cost-limit",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    run_comparison(args)


if __name__ == "__main__":
    main()
