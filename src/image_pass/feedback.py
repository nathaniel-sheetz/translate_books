"""
What the user said on the image board: ``.harness/images/feedback.json``.

The board is a page in the web UI, and this file is the only thing that page
writes. It holds two things per image, and neither is an instruction anything
acts on by itself:

- a **request** — "this is not a leave-alone, translate it", a note on how to
  transform the picture, a corrected label map, how many candidates to draw.
  The agent reads it when it writes the job.
- a **pick** — accept candidate N, redo, or skip. ``apply --from-board`` turns
  the outstanding ones into decisions.

Nothing here is ever marked as "handled". Whether a request still needs
attention is worked out by :mod:`src.image_pass.board` from what the prepared
job and the ledger say now, so a request that was folded into a job stops being
outstanding without anyone having to remember to tick it off.

A pick is tied to the picture it was made on: the job's prompt and, for an
accept, the candidate file's hash. A candidate regenerated under the same
number is a different picture, and a pick made on the old one must not land it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from src.image_pass import MODES, image_key, is_safe_key, work_dir
from src.image_pass import ledger
from src.image_pass.apply import VERDICT_ACCEPT, VERDICT_REDO, VERDICT_SKIP
from src.image_pass.jobs import MAX_CANDIDATES, candidate_path, load_manifest

# What triage, or the user over it, can say about an image: one of the job
# modes, or that it needs nothing.
VERDICT_LEAVE = "leave"
VERDICTS = MODES + (VERDICT_LEAVE,)

PICK_VERDICTS = (VERDICT_ACCEPT, VERDICT_REDO, VERDICT_SKIP)

# "Leave this section as it is", as distinct from None, which clears it.
KEEP: Any = object()

_NOTE_LIMIT = 4000

# The web UI serves requests on threads; two saves must not interleave their
# read-modify-write of the one file.
_WRITE_LOCK = threading.Lock()


def feedback_path(project_dir: Path) -> Path:
    return work_dir(project_dir) / "feedback.json"


def load(project_dir: Path) -> dict[str, dict[str, Any]]:
    """``{image key: {"request": {...}?, "pick": {...}?}}``; empty when unreadable."""
    try:
        doc = json.loads(feedback_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    images = doc.get("images") if isinstance(doc, dict) else None
    if not isinstance(images, dict):
        return {}
    return {str(key): row for key, row in images.items() if isinstance(row, dict)}


def clean_labels(labels: Any) -> tuple[dict[str, str], Optional[str]]:
    """A label map with its ends trimmed, or the reason it is not one."""
    if not isinstance(labels, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
        for k, v in labels.items()
    ):
        return {}, 'labels must map source text to its replacement: {"NORTH": "NORTE"}'
    return {k.strip(): v.strip() for k, v in labels.items()}, None


def _note(raw: Any, problems: list[str]) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        problems.append("note must be text")
        return ""
    if len(raw) > _NOTE_LIMIT:
        problems.append(f"note is longer than {_NOTE_LIMIT} characters")
    return raw.strip()


def validate_request(raw: Any) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Return ``(request, problems)``. An empty request comes back as ``None``:
    there is nothing to keep, so the section is cleared."""
    if not isinstance(raw, dict):
        return None, ["request is not an object"]
    problems: list[str] = []

    verdict = raw.get("verdict") or None
    if verdict is not None and verdict not in VERDICTS:
        problems.append(f"verdict {verdict!r} is not one of {list(VERDICTS)}")

    note = _note(raw.get("note"), problems)

    # None is "no opinion, use the job's or triage's map"; {} is "no lettering".
    labels = raw.get("labels")
    if labels is not None:
        labels, problem = clean_labels(labels)
        if problem:
            problems.append(problem)

    candidates = raw.get("candidates")
    if candidates is not None and (
        isinstance(candidates, bool)
        or not isinstance(candidates, int)
        or not (1 <= candidates <= MAX_CANDIDATES)
    ):
        problems.append(f"candidates must be an integer from 1 to {MAX_CANDIDATES}")

    if problems:
        return None, problems
    if verdict is None and not note and labels is None and candidates is None:
        return None, []
    return {"verdict": verdict, "note": note, "labels": labels, "candidates": candidates}, []


def validate_pick(
    project_dir: Path, image: str, raw: Any
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Return ``(pick, problems)``, stamped with the picture it was made on."""
    if not isinstance(raw, dict):
        return None, ["pick is not an object"]
    problems: list[str] = []
    verdict = raw.get("verdict")
    if verdict not in PICK_VERDICTS:
        return None, [f"verdict {verdict!r} is not one of {list(PICK_VERDICTS)}"]
    note = _note(raw.get("note"), problems)

    job = next(
        (job for job in load_manifest(project_dir)["jobs"] if job.get("image") == image), None
    )
    if job is None:
        return None, problems + ["no prepared job for this image: there is nothing to pick from"]

    candidate = None
    sha = None
    if verdict == VERDICT_ACCEPT:
        candidate = raw.get("candidate")
        if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 1:
            problems.append("an accept needs `candidate`: the 1-based number on the board")
        else:
            path = candidate_path(Path(job["job_dir"]), candidate)
            if not path.is_file():
                problems.append(f"candidate {candidate} does not exist")
            else:
                sha = ledger.sha256_file(path)
    if problems:
        return None, problems
    return {
        "verdict": verdict,
        "candidate": candidate,
        "candidate_sha256": sha,
        "prompt_sha": job.get("prompt_sha"),
        "note": note,
    }, []


def save_image(
    project_dir: Path,
    image: str,
    *,
    request: Any = KEEP,
    pick: Any = KEEP,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Set one image's request and/or pick and return ``{status, feedback}``.

    A section passed as ``None`` is cleared; one left at :data:`KEEP` is not
    touched, so a save from the label table cannot drop a pick made in another
    tab. Nothing is written unless everything given is valid.
    """
    project_dir = Path(project_dir)
    key = image_key(image)
    if not is_safe_key(key):
        return {"status": "error", "problems": [f"image {image!r} is not a path inside images/"]}

    problems: list[str] = []
    if request is not KEEP and request is not None:
        request, found = validate_request(request)
        problems += found
    if pick is not KEEP and pick is not None:
        pick, found = validate_pick(project_dir, key, pick)
        problems += found
    if problems:
        return {"status": "error", "problems": problems}

    stamp = now or ledger.now_stamp()
    with _WRITE_LOCK:
        images = load(project_dir)
        row = dict(images.get(key) or {})
        if request is not KEEP:
            if request is None:
                row.pop("request", None)
            else:
                before = row.get("request") or {}
                # Its own stamp, because "is this note newer than the job?" must
                # not flip when only the candidate count was touched.
                request["note_updated"] = (
                    before.get("note_updated") or stamp
                    if request["note"] == (before.get("note") or "")
                    else stamp
                )
                request["updated"] = stamp
                row["request"] = request
        if pick is not KEEP:
            if pick is None:
                row.pop("pick", None)
            else:
                pick["updated"] = stamp
                row["pick"] = pick
        if row:
            images[key] = row
        else:
            images.pop(key, None)

        path = feedback_path(project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(
            json.dumps({"version": 1, "updated": stamp, "images": images},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    return {"status": "ok", "feedback": row}
