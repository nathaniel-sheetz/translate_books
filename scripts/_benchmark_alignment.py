"""One-off benchmark script: re-run alignment on 50-famous with the new
code and write the output to a temp directory so we can compare against
the existing baseline files in projects/50-famous/alignments/.

Emits docs/alignment_benchmark_50_famous.md with before/after metrics.
"""

import json
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sentence_aligner import align_chapter_chunks  # noqa: E402


PROJECT = "50-famous"
PROJECT_DIR = Path("projects") / PROJECT
CHUNKS_DIR = PROJECT_DIR / "chunks"
BASELINE_DIR = PROJECT_DIR / "alignments"


def bucket(score: float) -> str:
    if score < 0.5:
        return "<0.5"
    if score < 0.7:
        return "0.5-0.7"
    if score < 0.85:
        return "0.7-0.85"
    return "0.85-1.0"


def metrics_for(rows: list[dict]) -> dict:
    if not rows:
        return {
            "rows": 0,
            "mean": 0.0,
            "median": 0.0,
            "low_conf": 0,
            "low_conf_pct": 0.0,
            "buckets": {k: 0 for k in ["<0.5", "0.5-0.7", "0.7-0.85", "0.85-1.0"]},
        }
    scores = [r["similarity"] for r in rows]
    low = sum(1 for s in scores if s < 0.7)
    buckets = defaultdict(int)
    for s in scores:
        buckets[bucket(s)] += 1
    return {
        "rows": len(rows),
        "mean": round(statistics.mean(scores), 4),
        "median": round(statistics.median(scores), 4),
        "low_conf": low,
        "low_conf_pct": round(low / len(rows) * 100, 2),
        "buckets": dict(buckets),
    }


def main():
    # Group chunks by chapter
    chunks_by_chapter: dict[str, list[str]] = defaultdict(list)
    for p in sorted(CHUNKS_DIR.glob("chapter_*_chunk_*.json")):
        chapter_id = p.stem.split("_chunk_")[0]
        chunks_by_chapter[chapter_id].append(str(p))

    print(f"Found {sum(len(v) for v in chunks_by_chapter.values())} chunks "
          f"across {len(chunks_by_chapter)} chapters")

    tmp_root = Path(tempfile.mkdtemp(prefix="align_bench_"))
    print(f"Writing new alignments to {tmp_root}")

    baseline_rows: list[dict] = []
    new_rows: list[dict] = []
    per_chapter = []

    t0 = time.time()
    for chapter_id in sorted(chunks_by_chapter.keys()):
        baseline_path = BASELINE_DIR / f"{chapter_id}.json"
        if not baseline_path.exists():
            # Chapters without baseline have no translated text; skip to keep
            # the comparison apples-to-apples.
            continue

        chunks = chunks_by_chapter[chapter_id]
        out_path = tmp_root / f"{chapter_id}.json"
        try:
            new_data = align_chapter_chunks(
                chunks,
                project_id=PROJECT,
                chapter_id=chapter_id,
                output_path=str(out_path),
            )
        except ValueError as e:
            print(f"  SKIP {chapter_id}: {e}")
            continue

        new_rows.extend(new_data["alignments"])

        baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_rows.extend(baseline_data["alignments"])

        per_chapter.append({
            "chapter": chapter_id,
            "baseline_mean": baseline_data.get("avg_similarity"),
            "baseline_high_pct": baseline_data.get("high_confidence_pct"),
            "baseline_rows": len(baseline_data["alignments"]),
            "new_mean": new_data.get("avg_similarity"),
            "new_high_pct": new_data.get("high_confidence_pct"),
            "new_rows": len(new_data["alignments"]),
        })
        print(f"  {chapter_id}: rows {len(baseline_data['alignments'])} -> "
              f"{len(new_data['alignments'])}, "
              f"mean {baseline_data.get('avg_similarity')} -> "
              f"{new_data.get('avg_similarity')}")

    elapsed = time.time() - t0
    print(f"\nAligned in {elapsed:.1f}s")

    baseline_metrics = metrics_for(baseline_rows)
    new_metrics = metrics_for(new_rows)

    # Pick spot-check: 10 baseline rows with similarity < 0.6, show after-state
    low_baseline = [r for r in baseline_rows if r["similarity"] < 0.6]
    # Sort so we get a spread of chapters
    low_baseline.sort(key=lambda r: (r.get("chunk_id", ""), r["es_idx"]))
    sample = low_baseline[: min(10, len(low_baseline))]

    # Build lookup of new rows by (chunk_id, en_idx) to find what happened
    new_by_key: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in new_rows:
        new_by_key[(r.get("chunk_id", ""), r["en_idx"])].append(r)

    spot_checks = []
    for b in sample:
        key = (b.get("chunk_id", ""), b["en_idx"])
        matches = new_by_key.get(key, [])
        if not matches:
            after_note = "(no matching en_idx in new output)"
            after_sim = None
        else:
            m = matches[0]
            merged = "es_indices" in m
            after_sim = m["similarity"]
            after_note = (
                f"merged {len(m['es_indices'])}->1" if merged else "unchanged row"
            )
        spot_checks.append({
            "chunk_id": b.get("chunk_id", ""),
            "en_idx": b["en_idx"],
            "es_idx": b["es_idx"],
            "en": b["en"],
            "es_before": b["es"],
            "sim_before": b["similarity"],
            "sim_after": after_sim,
            "note": after_note,
        })

    # Write report
    docs_dir = Path("docs")
    docs_dir.mkdir(exist_ok=True)

    lines = []
    lines.append("# 50-famous alignment benchmark — before vs after\n")
    lines.append("Comparison of alignment metrics on the `50-famous` project "
                 "before and after two fixes: N:1 dialogue grouping and "
                 "all-caps title case-normalization.\n")

    lines.append("## Overall metrics\n")
    lines.append("| metric | baseline | after | delta |")
    lines.append("|---|---|---|---|")
    lines.append(f"| total alignment rows | {baseline_metrics['rows']} | "
                 f"{new_metrics['rows']} | "
                 f"{new_metrics['rows'] - baseline_metrics['rows']:+d} |")
    lines.append(f"| mean similarity | {baseline_metrics['mean']} | "
                 f"{new_metrics['mean']} | "
                 f"{new_metrics['mean'] - baseline_metrics['mean']:+.4f} |")
    lines.append(f"| median similarity | {baseline_metrics['median']} | "
                 f"{new_metrics['median']} | "
                 f"{new_metrics['median'] - baseline_metrics['median']:+.4f} |")
    lines.append(f"| dashboard % (mean*100) | "
                 f"{round(baseline_metrics['mean']*100, 1)} | "
                 f"{round(new_metrics['mean']*100, 1)} | "
                 f"{round((new_metrics['mean']-baseline_metrics['mean'])*100, 1):+.1f} |")
    lines.append(f"| low-confidence rows (<0.7) | {baseline_metrics['low_conf']} | "
                 f"{new_metrics['low_conf']} | "
                 f"{new_metrics['low_conf'] - baseline_metrics['low_conf']:+d} |")
    lines.append(f"| low-confidence % | {baseline_metrics['low_conf_pct']}% | "
                 f"{new_metrics['low_conf_pct']}% | "
                 f"{new_metrics['low_conf_pct'] - baseline_metrics['low_conf_pct']:+.2f} |")
    lines.append("")

    lines.append("## Score-bucket distribution\n")
    lines.append("| bucket | baseline | after | delta |")
    lines.append("|---|---|---|---|")
    for b in ["<0.5", "0.5-0.7", "0.7-0.85", "0.85-1.0"]:
        bv = baseline_metrics["buckets"].get(b, 0)
        nv = new_metrics["buckets"].get(b, 0)
        lines.append(f"| {b} | {bv} | {nv} | {nv - bv:+d} |")
    lines.append("")

    lines.append("## Per-chapter (baseline → after)\n")
    lines.append("| chapter | rows | mean sim | high-conf % |")
    lines.append("|---|---|---|---|")
    for c in per_chapter:
        lines.append(
            f"| {c['chapter']} | {c['baseline_rows']} → {c['new_rows']} "
            f"| {c['baseline_mean']} → {c['new_mean']} "
            f"| {c['baseline_high_pct']}% → {c['new_high_pct']}% |"
        )
    lines.append("")

    lines.append("## Spot-check: 10 baseline rows with sim < 0.6\n")
    for i, s in enumerate(spot_checks, 1):
        lines.append(f"### {i}. {s['chunk_id']} · en_idx {s['en_idx']}")
        sa = f"{s['sim_after']:.3f}" if s['sim_after'] is not None else "n/a"
        lines.append(f"- **Before sim:** {s['sim_before']:.3f}  **After sim:** "
                     f"{sa}  ({s['note']})")
        lines.append(f"- **EN:** {s['en'][:160]}")
        lines.append(f"- **ES (before):** {s['es_before'][:160]}")
        lines.append("")

    (docs_dir / "alignment_benchmark_50_famous.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print(f"\nReport: docs/alignment_benchmark_50_famous.md")
    print(f"\nDASHBOARD % : {round(baseline_metrics['mean']*100, 1)}% -> "
          f"{round(new_metrics['mean']*100, 1)}%")


if __name__ == "__main__":
    main()
