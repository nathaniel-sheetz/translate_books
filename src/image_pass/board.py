"""
The image board: every image a book references, at whatever stage it is in.

One state, built here and shown by the web UI at ``/image-pass/<project>``. It
joins what five files say about each image:

- the inventory row (the token in ``source.txt`` and the file behind it),
- ``triage.json`` — what the agent made of the picture when it looked,
- the prepared job in ``manifest.json`` and the candidates in its folder,
- ``checks.json`` — what the agent found wrong with each candidate,
- the ledger (replaced, reverted, skipped, sent back), and
- ``feedback.json`` — what the user said on the page.

Nothing is cached and nothing is remembered between calls: the page shows what
is on disk now, which is the point of a page a published picture is approved
from.

Two of those files are written here, by the agent through the CLI: ``triage``
and ``check``. Like ``prepare``, both are all-or-nothing — one bad row refuses
the batch — and both merge by image, so a second look at three images leaves the
other eighty-four alone.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.image_pass import MODES, image_key, images_dir, is_safe_key, originals_dir, work_dir
from src.image_pass import feedback as fb
from src.image_pass import ledger
from src.image_pass.apply import (
    ASPECT_TOLERANCE,
    VERDICT_ACCEPT,
    VERDICT_REDO,
    VERDICT_SKIP,
    aspect_drift,
)
from src.image_pass.composite import load_sidecar
from src.image_pass.inventory import build_rows, probe_image
from src.image_pass.jobs import (
    candidate_models,
    candidate_path,
    existing_candidates,
    load_manifest,
)

STAGE_MISSING = "missing"
STAGE_REVIEW = "review"
STAGE_REPLACED = "replaced"
STAGE_QUEUED = "queued"
STAGE_PROPOSED = "proposed"
STAGE_LEAVE = "leave"
STAGE_UNTRIAGED = "untriaged"
# In the order the page lists them: what needs the user first.
STAGES = (
    STAGE_REVIEW,
    STAGE_PROPOSED,
    STAGE_QUEUED,
    STAGE_REPLACED,
    STAGE_MISSING,
    STAGE_UNTRIAGED,
    STAGE_LEAVE,
)

# Ledger actions that are someone's answer about an image. A backfill is not
# one: the picture is the publisher's before and after.
_DECISIONS = (
    ledger.ACTION_APPLY,
    ledger.ACTION_REVERT,
    ledger.ACTION_SKIP,
    ledger.ACTION_REDO,
)


def triage_path(project_dir: Path) -> Path:
    return work_dir(project_dir) / "triage.json"


def checks_path(project_dir: Path) -> Path:
    return work_dir(project_dir) / "checks.json"


def _load_keyed(path: Path, field: str) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = doc.get(field) if isinstance(doc, dict) else None
    return {str(k): v for k, v in rows.items() if isinstance(v, dict)} if isinstance(rows, dict) else {}


def _write_keyed(path: Path, field: str, rows: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps({"version": 1, "updated": ledger.now_stamp(), field: rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def load_triage(project_dir: Path) -> dict[str, dict[str, Any]]:
    return _load_keyed(triage_path(project_dir), "images")


def load_checks(project_dir: Path) -> dict[str, dict[str, Any]]:
    """``{image key: {candidate number as text: {ok, finding, sha256, updated}}}``."""
    return _load_keyed(checks_path(project_dir), "images")


def _refused(what: str, total: int, invalid: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "error",
        "error": f"{len(invalid)} of {total} {what} row(s) are invalid; nothing was recorded",
        "invalid": invalid,
        "counts": {"requested": total, "invalid": len(invalid), "recorded": 0},
        "instructions": "Fix the named problems and re-run with the whole batch.",
    }


# ---------------------------------------------------------------------------
# triage
# ---------------------------------------------------------------------------

def save_triage(project_dir: Path, rows: list[Any], *, replace: bool = False) -> dict[str, Any]:
    """Record what the agent made of each image it looked at. Spends nothing.

    A row is ``{image, verdict, finding, labels?, lettering?}``: the verdict is a
    job mode or ``leave``, and the finding is the sentence the user reads on the
    board to see why.
    """
    project_dir = Path(project_dir)
    if not rows:
        return {"status": "error", "error": "no triage rows given"}
    known = {row["image"] for row in build_rows(project_dir)[0]}

    valid: dict[str, dict[str, Any]] = {}
    invalid: list[dict[str, Any]] = []
    stamp = ledger.now_stamp()
    for position, raw in enumerate(rows):
        problems: list[str] = []
        key = image_key(raw.get("image") or "") if isinstance(raw, dict) else ""
        if not isinstance(raw, dict):
            problems.append("row is not an object")
        else:
            if not is_safe_key(key):
                problems.append(f"image {raw.get('image')!r} is not a path inside images/")
            elif key not in known:
                problems.append(f"{key}: the book does not reference this image")
            elif key in valid:
                problems.append(f"{key}: named twice in this batch")
            verdict = raw.get("verdict")
            if verdict not in fb.VERDICTS:
                problems.append(f"verdict {verdict!r} is not one of {list(fb.VERDICTS)}")
            finding = raw.get("finding")
            if not isinstance(finding, str) or not finding.strip():
                problems.append("finding is required: say what you saw in the picture")
            labels, problem = fb.clean_labels(raw.get("labels") or {})
            if problem:
                problems.append(problem)
            lettering = raw.get("lettering", bool(labels))
            if not isinstance(lettering, bool):
                problems.append("lettering must be true or false")
        if problems:
            invalid.append({
                "index": position,
                "image": raw.get("image") if isinstance(raw, dict) else None,
                "problems": problems,
            })
            continue
        valid[key] = {
            "verdict": verdict,
            "finding": finding.strip(),
            "labels": labels,
            "lettering": lettering,
            "updated": stamp,
        }

    if invalid:
        return _refused("triage", len(rows), invalid)

    merged = {} if replace else load_triage(project_dir)
    merged.update(valid)
    path = triage_path(project_dir)
    _write_keyed(path, "images", merged)

    by_verdict: dict[str, int] = {}
    for row in merged.values():
        by_verdict[row["verdict"]] = by_verdict.get(row["verdict"], 0) + 1
    return {
        "status": "ok",
        "counts": {
            "requested": len(rows),
            "recorded": len(valid),
            "invalid": 0,
            "triaged": len(merged),
            "untriaged": len(known - set(merged)),
            "by_verdict": by_verdict,
        },
        "triage_path": str(path),
        "instructions": (
            "Run `board` and give the user its url: they can correct a verdict or "
            "a label map, or say how they want an image done, before any job is "
            "written."
        ),
    }


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def save_checks(project_dir: Path, rows: list[Any]) -> dict[str, Any]:
    """Record what the agent found when it looked at each candidate.

    A row is ``{image, candidate, ok, finding}``. It is stored against the
    candidate file's hash, so when that number is generated again the finding
    is dropped with the picture it described.
    """
    project_dir = Path(project_dir)
    if not rows:
        return {"status": "error", "error": "no check rows given"}
    jobs = {job["image"]: job for job in load_manifest(project_dir)["jobs"]}

    valid: list[tuple[str, int, dict[str, Any]]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    stamp = ledger.now_stamp()
    for position, raw in enumerate(rows):
        problems: list[str] = []
        key = image_key(raw.get("image") or "") if isinstance(raw, dict) else ""
        candidate = raw.get("candidate") if isinstance(raw, dict) else None
        path: Optional[Path] = None
        if not isinstance(raw, dict):
            problems.append("row is not an object")
        else:
            job = jobs.get(key)
            if job is None:
                problems.append(f"{raw.get('image')!r}: no prepared job for this image")
            elif isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 1:
                problems.append("candidate must be the 1-based number of the candidate")
            else:
                path = candidate_path(Path(job["job_dir"]), candidate)
                if not path.is_file():
                    problems.append(f"{key}: candidate {candidate} does not exist")
                elif (key, candidate) in seen:
                    problems.append(f"{key}: candidate {candidate} named twice in this batch")
            ok = raw.get("ok")
            if not isinstance(ok, bool):
                problems.append("ok must be true or false")
            finding = raw.get("finding")
            if finding is not None and not isinstance(finding, str):
                problems.append("finding must be text")
            elif ok is False and not (finding or "").strip():
                problems.append("a candidate that is not ok needs a finding: say what is wrong")
        if problems:
            invalid.append({
                "index": position,
                "image": raw.get("image") if isinstance(raw, dict) else None,
                "problems": problems,
            })
            continue
        assert path is not None
        seen.add((key, candidate))
        valid.append((key, candidate, {
            "ok": ok,
            "finding": (finding or "").strip(),
            "sha256": ledger.sha256_file(path),
            "updated": stamp,
        }))

    if invalid:
        return _refused("check", len(rows), invalid)

    merged = load_checks(project_dir)
    for key, candidate, row in valid:
        merged.setdefault(key, {})[str(candidate)] = row
    path = checks_path(project_dir)
    _write_keyed(path, "images", merged)
    return {
        "status": "ok",
        "counts": {
            "requested": len(rows),
            "recorded": len(valid),
            "invalid": 0,
            "not_ok": sum(1 for _, _, row in valid if not row["ok"]),
        },
        "checks_path": str(path),
        "instructions": (
            "The findings now show under each candidate on the board. Give the "
            "user the board url and wait for their picks."
        ),
    }


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def _epoch(stamp: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return None


def _later(a: Any, b: Any) -> bool:
    """Whether stamp ``a`` is after stamp ``b``; an unreadable ``b`` is "never"."""
    first, second = _epoch(a), _epoch(b)
    if first is None:
        return False
    return second is None or first > second


def _picture(kind: str, path: Path, **extra: Any) -> dict[str, Any]:
    info = probe_image(path)
    return {
        "kind": kind,
        "path": str(path),
        "width": info.get("width"),
        "height": info.get("height"),
        # The page appends it to the picture's URL: a replacement keeps the
        # filename, and a browser would otherwise go on showing the old pixels.
        "v": int(path.stat().st_mtime),
        **extra,
    }


def _last_decisions(project_dir: Path) -> dict[str, dict[str, Any]]:
    last: dict[str, dict[str, Any]] = {}
    for row in ledger.read_rows(project_dir):
        if row.get("action") in _DECISIONS and row.get("image"):
            last[str(row["image"])] = row
    return last


def _drift(
    request: Optional[dict[str, Any]],
    triage: Optional[dict[str, Any]],
    job: Optional[dict[str, Any]],
) -> list[str]:
    """Why what the user asked for is not yet what the prepared job says."""
    if not request:
        return []
    reasons: list[str] = []
    wanted = request.get("verdict") or (job or {}).get("mode") or (triage or {}).get("verdict")
    note = request.get("note") or ""
    if job is None:
        if wanted in MODES:
            reasons.append("needs_job")
        elif note and _later(request.get("note_updated"), (triage or {}).get("updated")):
            reasons.append("note_unaddressed")
        return reasons
    if request.get("verdict") == fb.VERDICT_LEAVE:
        return ["job_unwanted"]
    if request.get("verdict") and request["verdict"] != job.get("mode"):
        reasons.append("mode_differs")
    if request.get("labels") is not None and request["labels"] != (job.get("labels") or {}):
        reasons.append("labels_differ")
    if request.get("candidates") and request["candidates"] != job.get("candidates"):
        reasons.append("candidates_differ")
    if note and _later(request.get("note_updated"), job.get("prepared_at")):
        reasons.append("note_newer_than_job")
    return reasons


def _pick_state(
    pick: Optional[dict[str, Any]],
    job: Optional[dict[str, Any]],
    decision: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """The pick with two facts added: is it still to be applied, and is it still
    about the picture it was made on."""
    if not pick:
        return None
    pending = _later(pick.get("updated"), (decision or {}).get("ts"))
    stale = job is None or pick.get("prompt_sha") != job.get("prompt_sha")
    if not stale and pick.get("verdict") == VERDICT_ACCEPT:
        path = candidate_path(Path(job["job_dir"]), int(pick.get("candidate") or 0))
        stale = not path.is_file() or ledger.sha256_file(path) != pick.get("candidate_sha256")
    return {**pick, "pending": pending, "stale": pending and stale}


def _owed(job: dict[str, Any], candidates: list[dict[str, Any]]) -> int:
    """How many of the job's candidates have no file yet, as ``generate`` counts
    them. A composite is numbered past the job's own and fills none of them."""
    have = {c["candidate"] for c in candidates}
    return sum(1 for n in range(1, (job.get("candidates") or 1) + 1) if n not in have)


def _stage(
    row: dict[str, Any],
    verdict: Optional[str],
    job: Optional[dict[str, Any]],
    candidates: list[dict[str, Any]],
    decision: Optional[dict[str, Any]],
) -> str:
    if row["missing"] and row["role"] != "cover" and job is None:
        return STAGE_MISSING
    decided_at = _epoch((decision or {}).get("ts"))
    if candidates and (decided_at is None or max(c["v"] for c in candidates) > decided_at):
        return STAGE_REVIEW
    if row["status"] == ledger.STATUS_REPLACED:
        return STAGE_REPLACED
    if job is not None and _owed(job, candidates):
        return STAGE_QUEUED
    action = (decision or {}).get("action")
    if job is not None and action == ledger.ACTION_REDO:
        return STAGE_PROPOSED
    if job is not None and action in (ledger.ACTION_SKIP, ledger.ACTION_REVERT):
        return STAGE_LEAVE
    if verdict in MODES:
        return STAGE_PROPOSED
    if verdict == fb.VERDICT_LEAVE:
        return STAGE_LEAVE
    return STAGE_UNTRIAGED


def build(project_dir: Path) -> dict[str, Any]:
    """One row per image, joined across every file that says something about it."""
    project_dir = Path(project_dir)
    rows, unreferenced, _cover = build_rows(project_dir)
    triage = load_triage(project_dir)
    checks = load_checks(project_dir)
    feedback = fb.load(project_dir)
    decisions = _last_decisions(project_dir)
    jobs = {job["image"]: job for job in load_manifest(project_dir)["jobs"]}
    made_by = candidate_models(project_dir)

    images: list[dict[str, Any]] = []
    counts = {stage: 0 for stage in STAGES}
    for row in rows:
        key = row["image"]
        job = jobs.get(key)
        tri = triage.get(key)
        said = feedback.get(key) or {}
        request = said.get("request")
        decision = decisions.get(key)

        backup = originals_dir(project_dir) / key
        current = images_dir(project_dir) / key
        pictures: list[dict[str, Any]] = []
        original: Optional[dict[str, Any]] = None
        if backup.is_file():
            original = _picture("original", backup, source="images_original", key=key)
            pictures.append(original)
            if current.is_file() and row["status"] == ledger.STATUS_REPLACED:
                pictures.append(_picture("current", current, source="images", key=key))
        elif current.is_file():
            original = _picture("original", current, source="images", key=key)
            pictures.append(original)
        if job and job.get("reference") and job.get("input") and Path(job["input"]).is_file():
            drawn_from = Path(job["input"])
            ref_backup = originals_dir(project_dir) / job["reference"]
            from_backup = ref_backup.is_file() and ref_backup.resolve() == drawn_from.resolve()
            pictures.append(_picture(
                "reference", drawn_from,
                source="images_original" if from_backup else "images", key=job["reference"],
            ))

        candidates: list[dict[str, Any]] = []
        for number in existing_candidates(Path(job["job_dir"])) if job else []:
            path = candidate_path(Path(job["job_dir"]), number)
            picture = _picture("candidate", path, job_id=job["id"], candidate=number)
            flags: list[str] = []
            if picture["width"] is None:
                flags.append("unreadable")
            elif original and original["width"]:
                drift = aspect_drift(
                    picture["width"], picture["height"], original["width"], original["height"]
                )
                if drift > ASPECT_TOLERANCE:
                    flags.append(f"aspect {drift:.0%} off the original")
            # A finding is about one file. Regenerated under the same number,
            # the candidate is a picture nobody has checked yet.
            check = (checks.get(key) or {}).get(str(number))
            if check and check.get("sha256") != ledger.sha256_file(path):
                check = None
            # A composite says what it was made of and where the join runs, so
            # the page can draw the outline over it.
            made_from = load_sidecar(path)
            picture["composite"] = {
                "from": {k: v for k, v in made_from["from"].items() if k != "sha256"},
                "base": {k: v for k, v in made_from["base"].items() if k != "sha256"},
                "regions": made_from.get("regions") or [],
                "changed_share": made_from.get("changed_share"),
            } if made_from else None
            # Two models can fill the candidates of one job; which made this one
            # is otherwise only in the usage log.
            picture["model"] = None if made_from else made_by.get((job["id"], number))
            picture["flags"] = flags
            picture["check"] = (
                {"ok": check.get("ok"), "finding": check.get("finding") or ""} if check else None
            )
            candidates.append(picture)

        verdict = (request or {}).get("verdict") or (job or {}).get("mode") or (tri or {}).get("verdict")
        labels = (
            request["labels"] if request and request.get("labels") is not None
            else (job or {}).get("labels") if job
            else (tri or {}).get("labels")
        ) or {}
        stage = _stage(row, verdict, job, candidates, decision)
        counts[stage] += 1
        pick = _pick_state(said.get("pick"), job, decision)

        flagged: list[str] = []
        if row["missing"] and row["role"] != "cover":
            flagged.append("missing_file")
        if row.get("error"):
            flagged.append("unreadable")
        if stage == STAGE_REVIEW:
            if any(c["flags"] for c in candidates):
                flagged.append("candidate_flag")
            if any(c["check"] and c["check"]["ok"] is False for c in candidates):
                flagged.append("check_failed")
        if pick and pick["stale"]:
            flagged.append("stale_pick")

        images.append({
            "image": key,
            "role": row["role"],
            "chapter": row.get("chapter"),
            "alt": row.get("alt"),
            "missing": row["missing"],
            "width": row.get("width"),
            "height": row.get("height"),
            "status": row["status"],
            "stage": stage,
            "verdict": verdict,
            "labels": labels,
            "lettering": bool(labels) or bool((tri or {}).get("lettering")),
            "flagged": flagged,
            "triage": tri,
            "job": {
                field: job.get(field)
                for field in ("id", "mode", "instruction", "labels", "candidates",
                              "model", "reference", "input_from", "prepared_at")
            } if job else None,
            "pictures": pictures,
            "candidates": candidates,
            "decision": {
                field: decision.get(field) for field in ("action", "ts", "note", "candidate")
            } if decision else None,
            "request": request,
            "drift": _drift(request, tri, job),
            "pick": pick,
        })

    return {
        "status": "ok",
        "project": project_dir.name,
        "images": images,
        "unreferenced": unreferenced,
        "counts": {
            "images": len(images),
            **counts,
            "lettering": sum(1 for image in images if image["lettering"]),
            "with_input": sum(1 for image in images if image["request"] or image["pick"]),
            "flagged": sum(1 for image in images if image["flagged"]),
        },
    }


# ---------------------------------------------------------------------------
# what the next step reads
# ---------------------------------------------------------------------------

def decisions_from_picks(board: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(decisions, stale)``: the picks still to be applied, as ``apply`` rows,
    and the ones made on a picture that has since been replaced."""
    decisions: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for image in board["images"]:
        pick = image.get("pick")
        if not pick or not pick["pending"]:
            continue
        if pick["stale"]:
            stale.append({
                "image": image["image"],
                "candidate": pick.get("candidate"),
                "problems": [
                    "this pick was made on a candidate that has since been "
                    "regenerated or re-prepared: pick again on the board"
                ],
            })
            continue
        row: dict[str, Any] = {"image": image["image"], "note": pick.get("note") or None}
        if pick["verdict"] == VERDICT_ACCEPT:
            row["candidate"] = pick["candidate"]
        else:
            row["verdict"] = VERDICT_REDO if pick["verdict"] == VERDICT_REDO else VERDICT_SKIP
        decisions.append(row)
    return decisions, stale


def summary(project_dir: Path) -> dict[str, Any]:
    """What an agent needs from the board without 87 rows in its context: the
    candidates to look at, what the user asked for that no job reflects yet, and
    the picks waiting to be applied."""
    board = build(project_dir)
    jobs: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    proposed: list[dict[str, Any]] = []
    for image in board["images"]:
        job = image["job"]
        if job is None and image["stage"] == STAGE_PROPOSED:
            # Whose verdict it is matters: an image the user moved out of
            # "leave alone" is one the triage has no finding or labels for.
            mine = (image["request"] or {}).get("verdict")
            proposed.append({
                "image": image["image"],
                "verdict": image["verdict"],
                "by": "user" if mine else "triage",
            })
        if job:
            original = next((p for p in image["pictures"] if p["kind"] == "original"), None)
            jobs.append({
                "id": job["id"],
                "image": image["image"],
                "mode": job["mode"],
                "stage": image["stage"],
                "original": original["path"] if original else None,
                "original_size": (
                    [original["width"], original["height"]]
                    if original and original["width"] else None
                ),
                "reference": job.get("reference"),
                "labels": job.get("labels") or {},
                "candidates": [
                    {
                        "candidate": c["candidate"],
                        "path": c["path"],
                        "size": [c["width"], c["height"]] if c["width"] else None,
                        "flags": c["flags"],
                        "checked": c["check"] is not None,
                        "composite": c["composite"],
                        "model": c["model"],
                    }
                    for c in image["candidates"]
                ],
                "missing": _owed(job, image["candidates"]),
            })
        if image["drift"]:
            request = image["request"]
            requests.append({
                "image": image["image"],
                "reasons": image["drift"],
                "verdict": request.get("verdict"),
                "note": request.get("note") or None,
                "labels": request.get("labels"),
                "candidates": request.get("candidates"),
                "triage_verdict": (image["triage"] or {}).get("verdict"),
                "job_mode": (job or {}).get("mode"),
            })
    decisions, stale = decisions_from_picks(board)
    return {
        "status": "ok",
        "project": board["project"],
        "counts": {
            **board["counts"],
            "requests": len(requests),
            "picks": len(decisions),
            "stale_picks": len(stale),
            "candidates": sum(len(job["candidates"]) for job in jobs),
            "unchecked_candidates": sum(
                1 for job in jobs if job["stage"] == STAGE_REVIEW
                for c in job["candidates"] if not c["checked"]
            ),
        },
        "untriaged": [i["image"] for i in board["images"] if i["stage"] == STAGE_UNTRIAGED],
        "proposed": proposed,
        "jobs": jobs,
        "requests": requests,
        "picks": decisions,
        "stale_picks": stale,
    }
