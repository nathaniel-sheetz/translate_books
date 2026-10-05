"""
The review page: each original beside the candidates generated for it.

One static HTML file, ``.harness/images/review.html``, opened straight from
disk. It links the images by relative path rather than embedding them, so it
needs no image library and no server — and it always shows what is on disk now,
which is the point of a page a human approves a published picture from.
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any, Optional

from src.image_pass import images_dir, originals_dir, work_dir
from src.image_pass import ledger
from src.image_pass.apply import ASPECT_TOLERANCE, aspect_drift
from src.image_pass.inventory import probe_image
from src.image_pass.jobs import candidate_path, existing_candidates, load_manifest

_CSS = """
body { font: 15px/1.45 system-ui, sans-serif; margin: 2rem; color: #1b1b1b; background: #f6f5f2; }
h1 { font-size: 1.3rem; }
section { background: #fff; border: 1px solid #d9d6cf; border-radius: 6px; padding: 1rem 1.25rem; margin: 0 0 1.5rem; }
h2 { font-size: 1.05rem; margin: 0 0 .25rem; }
.meta { color: #5a5750; margin: 0 0 .75rem; }
.row { display: flex; flex-wrap: wrap; gap: 1rem; align-items: flex-start; }
figure { margin: 0; flex: 1 1 320px; max-width: 520px; }
figure img { max-width: 100%; height: auto; border: 1px solid #c9c5bc; background: #fff; }
figcaption { font-size: .85rem; color: #5a5750; margin-top: .25rem; }
.warn { color: #8a3b00; font-weight: 600; }
table { border-collapse: collapse; margin: .25rem 0 .75rem; }
td { border: 1px solid #d9d6cf; padding: .15rem .5rem; }
pre { white-space: pre-wrap; margin: .25rem 0 .75rem; font: inherit; }
"""


def _rel(path: Path, start: Path) -> str:
    return Path(os.path.relpath(path, start)).as_posix()


def _size(info: dict[str, Any]) -> str:
    return f"{info['width']}×{info['height']}" if info.get("width") else "size unknown"


def review(project_dir: Path) -> dict[str, Any]:
    """Write the review page and return the paths an agent should Read."""
    project_dir = Path(project_dir)
    manifest = load_manifest(project_dir)
    if not manifest["jobs"]:
        return {"status": "error", "error": "no prepared jobs — run `prepare` first"}

    out_dir = work_dir(project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    state = ledger.current_state(project_dir)
    parts: list[str] = []
    jobs_out: list[dict[str, Any]] = []
    total = 0

    for job in manifest["jobs"]:
        key = job["image"]
        job_dir = Path(job["job_dir"])
        backup = originals_dir(project_dir) / key
        current = images_dir(project_dir) / key
        original: Optional[Path] = backup if backup.is_file() else current if current.is_file() else None
        orig_info = probe_image(original) if original else {}
        numbers = existing_candidates(job_dir)
        total += len(numbers)

        figures: list[str] = []
        if original is not None:
            figures.append(
                f'<figure><img src="{html.escape(_rel(original, out_dir))}" alt="original">'
                f"<figcaption>Original · {html.escape(_size(orig_info))}</figcaption></figure>"
            )
        else:
            figures.append("<figure><figcaption>No original (new image)</figcaption></figure>")
        drawn_from = Path(job["input"]) if job.get("reference") and job.get("input") else None
        if drawn_from is not None and drawn_from.is_file():
            figures.append(
                f'<figure><img src="{html.escape(_rel(drawn_from, out_dir))}" alt="reference">'
                f"<figcaption>Drawn from {html.escape(job['reference'])} · "
                f"{html.escape(_size(probe_image(drawn_from)))}</figcaption></figure>"
            )

        cands_out: list[dict[str, Any]] = []
        for number in numbers:
            path = candidate_path(job_dir, number)
            info = probe_image(path)
            drift = (
                aspect_drift(info["width"], info["height"], orig_info["width"], orig_info["height"])
                if info.get("width") and orig_info.get("width")
                else 0.0
            )
            flags: list[str] = []
            if info.get("error"):
                flags.append("unreadable")
            if drift > ASPECT_TOLERANCE:
                flags.append(f"aspect {drift:.0%} off the original")
            warn = f' <span class="warn">— {html.escape("; ".join(flags))}</span>' if flags else ""
            figures.append(
                f'<figure><img src="{html.escape(_rel(path, out_dir))}" alt="candidate {number}">'
                f"<figcaption>Candidate {number} · {html.escape(_size(info))}{warn}</figcaption></figure>"
            )
            cands_out.append({
                "candidate": number,
                "path": str(path),
                "size": [info["width"], info["height"]] if info.get("width") else None,
                "flags": flags,
            })

        labels = job.get("labels") or {}
        table = (
            "<table>"
            + "".join(
                f"<tr><td>{html.escape(src)}</td><td>{html.escape(dst)}</td></tr>"
                for src, dst in labels.items()
            )
            + "</table>"
            if labels
            else ""
        )
        status = ledger.status_of(state.get(key))
        parts.append(
            "<section>"
            f"<h2>{html.escape(key)}</h2>"
            f'<p class="meta">{html.escape(job["mode"])}'
            f" · {len(numbers)} of {job['candidates']} candidate(s)"
            + (f" · currently {html.escape(status)}" if status else "")
            + "</p>"
            f"<pre>{html.escape(job['instruction'])}</pre>"
            f"{table}"
            f'<div class="row">{"".join(figures)}</div>'
            "</section>"
        )
        jobs_out.append({
            "id": job["id"],
            "image": key,
            "mode": job["mode"],
            "original": str(original) if original else None,
            "reference": job.get("reference"),
            "original_size": [orig_info["width"], orig_info["height"]] if orig_info.get("width") else None,
            "labels": labels,
            "candidates": cands_out,
            "missing": max(0, job["candidates"] - len(numbers)),
        })

    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>Image review — {html.escape(project_dir.name)}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>Image review — {html.escape(project_dir.name)}</h1>"
        f"{''.join(parts)}</body></html>\n"
    )
    out_path = out_dir / "review.html"
    out_path.write_text(page, encoding="utf-8")

    return {
        "status": "ok",
        "review_path": str(out_path),
        "jobs": jobs_out,
        "counts": {
            "jobs": len(jobs_out),
            "candidates": total,
            "jobs_without_candidates": sum(1 for job in jobs_out if not job["candidates"]),
        },
        "instructions": (
            "Give the user review_path to open in a browser. Read every "
            "candidate path yourself beside its original and report garbled "
            "lettering, a wrong or missing label, or changed artwork before "
            "asking for picks."
        ),
    }
