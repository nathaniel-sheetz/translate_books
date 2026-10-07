"""
Image jobs: ``prepare`` validates and renders them, ``generate`` runs them.

A job is one image and what to do to it. The orchestrating agent writes it after
*looking* at the image — Codex only makes pixels — so everything a job needs is
decided before any usage is spent: the mode, the instruction, and for a
lettering translation the exact English → target label map a human approved.

``prepare`` spends nothing and is all-or-nothing: one invalid job refuses the
batch, because a batch is approved as a whole and half of one is not what was
agreed. ``generate`` is the opposite — one failed candidate never costs the
others, since each one is minutes of someone's plan limit.

Layout, under ``<project>/.harness/images/``::

    manifest.json                 every prepared job
    usage.jsonl                   one row per Codex run (wall time, outcome)
    jobs/<id>/job.json            the validated job, as prepared
    jobs/<id>/prompt.txt          the rendered prompt Codex receives on stdin
    jobs/<id>/cand_NN.png         harvested candidates, 1-based; a composite
                                  (see composite.py) is numbered after them and
                                  has a cand_NN.composite.json beside it
    jobs/<id>/run_NN/             that candidate's Codex working directory: the
                                  input copy, events.jsonl, stderr.txt
    jobs/<id>/previous/<stamp>/   candidates of an earlier prompt, never deleted
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from src.harness import headless, locks
from src.image_pass import (
    COVER_NAMES,
    IMAGE_SUFFIXES,
    MODE_COVER,
    MODE_TRANSLATE,
    MODES,
    image_key,
    images_dir,
    is_safe_key,
    jobs_dir,
    originals_dir,
    work_dir,
)
from src.image_pass import ledger
from src.image_pass.inventory import find_cover, probe_image

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "image_pass"

DEFAULT_CANDIDATES = 1
MAX_CANDIDATES = 4

# Ten Codex processes at once ran 40 jobs on the real CLI with none failed and
# every picture harvested to its own job (kittens-and-cats, 2026-10-06). The
# plan's window is spent per image, not per minute, so running them one at a
# time saved no usage and cost 14 minutes of waiting on the run before it.
DEFAULT_CONCURRENCY = 10

# What `--model` falls back to when a book pins one (`harness.py config-set
# --key image_model`).
MODEL_CONFIG_KEY = "image_model"
DEFAULT_MODEL_LABEL = "(Codex default, from ~/.codex/config.toml)"
# A model id ends up on a child argv, which on Windows is re-parsed by cmd.exe
# when `codex` resolves to a .CMD shim.
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")

INPUT_ORIGINAL = "original"
INPUT_CURRENT = "current"
# A cover drawn from another of the book's images: that image is attached as
# source material, not as the thing being edited.
INPUT_REFERENCE = "reference"

# A file of this name in the run dir is harvested if one ever appears, but the
# prompt no longer asks for it: see _CONTRACT_EDIT.
OUTPUT_NAME = "output.png"

# Used by ``--estimate`` until a project has measured a run of its own. One
# generation took 85 s on the first live run (2026-10-05); the margin is for a
# slower day, not for a model that regenerates.
ASSUMED_MINUTES_PER_IMAGE = 2.0

LIMIT_WARNING = (
    "Image generation draws on the ChatGPT plan's usage limits far faster than "
    "text: budget each image at roughly 3-5x an ordinary Codex turn. A large "
    "batch can exhaust the plan's window part-way through; candidates already "
    "harvested are kept, and re-running `generate` picks up where it stopped."
)

_JOB_KEYS = frozenset(
    {"image", "mode", "instruction", "labels", "candidates", "input", "reference", "note",
     "model"}
)

# The first live run (2026-10-05, codex-cli 0.157.0) asked the model to save the
# result as output.png. It could not: the tool hands back image data with no
# path, the sandboxed shell does not see CODEX_HOME, and piping 1.8 MB of base64
# through a command line fails with Windows error 206. It regenerated the image
# four times looking for a way, at four times the plan usage. Codex already
# writes every result to $CODEX_HOME/generated_images/<thread id>/, so the
# contract is now "call the tool once and stop" and the harvest reads that
# folder.
_CONTRACT_TAIL = """- Call the tool exactly once, then stop. One call is one image; do not generate a second version for any reason.
- Do not save, copy, move, resize, crop or re-encode the result, and do not run any shell command. The image is collected automatically from Codex's own output folder; the pixel size it comes back at is fine.
- End with one short line saying the image is done."""

_CONTRACT_EDIT = """Output:
- Use the built-in image generation tool to edit `{input_name}`, the attached image (it is also in the working directory). Never use the CLI fallback or any script that needs an API key; if the built-in tool is unavailable, stop and say so.
- Keep the original's proportions (it is {width} wide by {height} high).
""" + _CONTRACT_TAIL

# No "keep the original's proportions" here: the cover is a new picture in a
# cover's shape, recomposed from an illustration that is usually landscape.
_CONTRACT_REFERENCE = """Output:
- Use the built-in image generation tool, with `{input_name}`, the attached image, as its source (it is also in the working directory). Never use the CLI fallback or any script that needs an API key; if the built-in tool is unavailable, stop and say so.
- The result is a new picture in a cover's portrait proportions, not the attached image's ({width} wide by {height} high).
""" + _CONTRACT_TAIL

_CONTRACT_NEW = """Output:
- Use the built-in image generation tool. Never use the CLI fallback or any script that needs an API key; if the built-in tool is unavailable, stop and say so.
""" + _CONTRACT_TAIL


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def manifest_path(project_dir: Path) -> Path:
    return work_dir(project_dir) / "manifest.json"


def usage_path(project_dir: Path) -> Path:
    return work_dir(project_dir) / "usage.jsonl"


def job_id_for(key: str) -> str:
    """A directory-safe id for an image key (``maps/001.jpg`` → ``maps_001.jpg``)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key)


def _read_manifest(path: Path) -> Optional[dict[str, Any]]:
    """The manifest at ``path``, or ``None`` when what is there is not one."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), list):
        return None
    doc["jobs"] = [job for job in doc["jobs"] if isinstance(job, dict) and job.get("id")]
    return doc


def load_manifest(project_dir: Path) -> dict[str, Any]:
    path = manifest_path(project_dir)
    doc = _read_manifest(path) if path.exists() else None
    return doc if doc is not None else {"version": 1, "jobs": []}


def _write_atomic(path: Path, text: str) -> None:
    """Whole or not at all: neither a reader nor a kill finds half a file."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _book_meta(project_dir: Path) -> dict[str, str]:
    """Title, author and target language, from wherever the project keeps them."""
    meta = {"title": "", "author": "", "target_language": ""}
    for name in (Path(".harness") / "config.json", Path("project.json")):
        path = Path(project_dir) / name
        if not path.exists():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(doc, dict):
            continue
        for key in meta:
            value = doc.get(key)
            if not meta[key] and isinstance(value, str) and value.strip():
                meta[key] = value.strip()
    meta["title"] = meta["title"] or Path(project_dir).name
    meta["target_language"] = meta["target_language"] or "Spanish"
    return meta


def split_models(value: Any) -> tuple[Optional[list[str]], Optional[str]]:
    """``"a,b"`` or ``["a", "b"]`` as ``(model ids, problem)``.

    ``(None, None)`` when no model is named. More than one id is a rotation:
    candidate 1 runs on the first, candidate 2 on the second, and round again.
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        ids = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        ids = [v.strip() for v in value if v.strip()]
    else:
        return None, 'model must be a model id or a list of them: "gpt-6-luna"'
    bad = [model for model in ids if not _MODEL_ID_RE.fullmatch(model)]
    if bad:
        return None, f"model id(s) {bad} are not plain ids (letters, digits, . _ : / -)"
    return ids or None, None


def pinned_models(project_dir: Path) -> tuple[Optional[list[str]], Optional[str]]:
    """The model(s) this book pins for image jobs, as ``(model ids, problem)``.
    ``(None, None)`` when it pins none."""
    try:
        doc = json.loads(
            (Path(project_dir) / ".harness" / "config.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None, None
    return split_models(doc.get(MODEL_CONFIG_KEY) if isinstance(doc, dict) else None)


def _render_labels(labels: dict[str, str]) -> str:
    return "\n".join(f'- "{source}" → "{target}"' for source, target in labels.items())


def load_template(mode: str) -> str:
    return (PROMPTS_DIR / f"{mode}.txt").read_text(encoding="utf-8")


def render_prompt(job: dict[str, Any], meta: dict[str, str]) -> str:
    """Fill ``prompts/image_pass/<mode>.txt`` for one validated job."""
    labels = job.get("labels") or {}
    input_name = job.get("input_name")
    if input_name and job.get("reference"):
        contract = _CONTRACT_REFERENCE.format(
            input_name=input_name,
            width=job.get("width"),
            height=job.get("height"),
        )
        cover_task = (
            f"Create the front cover of a book from the attached image, `{input_name}`, "
            "one of the book's own illustrations, following the editor's brief below."
        )
    elif input_name:
        contract = _CONTRACT_EDIT.format(
            input_name=input_name,
            width=job.get("width"),
            height=job.get("height"),
        )
        cover_task = (
            f"Edit the attached image, `{input_name}`, the front cover of a book, "
            "following the editor's brief below."
        )
    else:
        contract = _CONTRACT_NEW
        cover_task = "Create the front cover of a book, following the editor's brief below."
    labels_block = (
        "\nLettering, spelled exactly:\n" + _render_labels(labels) + "\n" if labels else ""
    )
    values = {
        "input_name": input_name or "",
        "title": meta["title"],
        "author_clause": f", by {meta['author']}" if meta["author"] else "",
        "target_language": meta["target_language"],
        "labels": _render_labels(labels),
        "labels_block": labels_block,
        "instruction": job["instruction"],
        "cover_task": cover_task,
        "contract": contract,
    }
    text = load_template(job["mode"])
    # Single pass over the template's own placeholders: an instruction that
    # happens to contain "{{title}}" is the editor's text, not a slot.
    missing: list[str] = []

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            missing.append(name)
            return match.group(0)
        return values[name]

    rendered = re.sub(r"\{\{(\w+)\}\}", fill, text)
    if missing:
        raise ValueError(
            f"prompts/image_pass/{job['mode']}.txt uses unknown placeholder(s): "
            + ", ".join(sorted(set(missing)))
        )
    return rendered.strip() + "\n"


def _validate_job(
    project_dir: Path, raw: Any, state: dict[str, dict], cover: Optional[str]
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Return ``(job, problems)``; ``job`` is ``None`` when there are problems."""
    if not isinstance(raw, dict):
        return None, ["job is not an object"]
    problems: list[str] = []
    unknown = sorted(set(raw) - _JOB_KEYS)
    if unknown:
        problems.append(
            f"unknown key(s) {unknown} — a job takes {sorted(_JOB_KEYS)}"
        )

    key = image_key(raw.get("image") or "")
    if not is_safe_key(key):
        return None, problems + [f"image {raw.get('image')!r} is not a path inside images/"]
    if Path(key).suffix.lower() not in IMAGE_SUFFIXES:
        problems.append(f"{key}: not an image file ({', '.join(IMAGE_SUFFIXES)})")

    mode = raw.get("mode")
    if mode not in MODES:
        problems.append(f"mode {mode!r} is not one of {list(MODES)}")

    instruction = raw.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        problems.append("instruction is required: say what this image needs")

    labels = raw.get("labels")
    if labels is None:
        labels = {}
    if not isinstance(labels, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
        for k, v in labels.items()
    ):
        problems.append('labels must map source text to its replacement: {"NORTH": "NORTE"}')
        labels = {}
    if mode == MODE_TRANSLATE and not labels:
        problems.append(
            "translate needs a labels map — list each English label in the image "
            "and the approved replacement"
        )

    candidates = raw.get("candidates", DEFAULT_CANDIDATES)
    if isinstance(candidates, bool) or not isinstance(candidates, int) or not (
        1 <= candidates <= MAX_CANDIDATES
    ):
        problems.append(f"candidates must be an integer from 1 to {MAX_CANDIDATES}")
        candidates = DEFAULT_CANDIDATES

    models, model_problem = split_models(raw.get("model"))
    if model_problem:
        problems.append(model_problem)

    which = raw.get("input", INPUT_ORIGINAL)
    if which not in (INPUT_ORIGINAL, INPUT_CURRENT):
        problems.append(f"input must be {INPUT_ORIGINAL!r} or {INPUT_CURRENT!r}")
        which = INPUT_ORIGINAL

    current = images_dir(project_dir) / key
    backup = originals_dir(project_dir) / key
    # Always work from the publisher's pixels unless told otherwise: a redo of
    # an already-replaced image must not compound one generation's errors
    # into the next.
    source: Optional[Path] = None
    if which == INPUT_ORIGINAL and backup.is_file():
        source = backup
    elif current.is_file() and not (
        # A cover image-pass made from nothing has no original either: asked
        # for the original, a redo starts from nothing again.
        which == INPUT_ORIGINAL and ledger.made_from_nothing(state.get(key), current)
    ):
        source = current

    reference: Optional[str] = None
    if raw.get("reference") is not None:
        wanted = image_key(raw["reference"]) if isinstance(raw["reference"], str) else ""
        if mode != MODE_COVER:
            problems.append(
                "reference is for a cover job: the image a new cover is drawn from"
            )
        elif not is_safe_key(wanted):
            problems.append(f"reference {raw['reference']!r} is not a path inside images/")
        else:
            ref_backup = originals_dir(project_dir) / wanted
            ref_current = images_dir(project_dir) / wanted
            # Same rule as a job's own input: the publisher's picture unless told
            # otherwise, so a cover is not drawn from a generated replacement.
            if which == INPUT_ORIGINAL and ref_backup.is_file():
                source, reference = ref_backup, wanted
            elif ref_current.is_file():
                source, reference = ref_current, wanted
            else:
                problems.append(f"reference {wanted}: no such file in images/")

    if mode == MODE_COVER:
        if key not in COVER_NAMES:
            problems.append(
                f"a cover job must target one of {list(COVER_NAMES)} — the names "
                "the EPUB builder auto-detects"
            )
        elif cover and cover != key:
            problems.append(
                f"this book's cover is {cover}; target that, or {key} would "
                "shadow or be shadowed by it"
            )
    elif source is None:
        problems.append(f"{key}: no such file in images/")

    width = height = None
    if source is not None:
        info = probe_image(source)
        if info.get("error"):
            problems.append(f"{key}: {info['error']}")
        else:
            width, height = info["width"], info["height"]

    if problems:
        return None, problems

    return {
        "id": job_id_for(key),
        "image": key,
        "mode": mode,
        "instruction": instruction.strip(),
        "labels": {k.strip(): v.strip() for k, v in labels.items()},
        "candidates": candidates,
        # Not part of the prompt, so naming another model archives nothing: the
        # candidates already made stay, and only a missing one runs on it.
        "model": models,
        "note": raw.get("note") if isinstance(raw.get("note"), str) else None,
        "input": str(source) if source is not None else None,
        "input_asked": which,
        "input_from": (
            None if source is None
            else INPUT_REFERENCE if reference
            else INPUT_ORIGINAL if source == backup or not backup.is_file()
            else INPUT_CURRENT
        ),
        "input_name": (
            None if source is None
            else f"{'reference' if reference else 'input'}{source.suffix.lower()}"
        ),
        "reference": reference,
        "width": width,
        "height": height,
        "status_at_prepare": ledger.status_of(state.get(key)),
    }, []


def _input_path(project_dir: Path, job: dict[str, Any]) -> Path:
    """Where a job's input is read from now. The path recorded at prepare time
    goes stale: once a candidate is applied, ``images/<key>`` is the replacement
    and the publisher's file is the backup that apply made."""
    recorded = Path(job["input"])
    # Jobs prepared before ``input_asked`` was recorded: only a plain original
    # is known to have asked for one.
    asked = job.get("input_asked") or (
        INPUT_ORIGINAL if job.get("input_from") == INPUT_ORIGINAL else INPUT_CURRENT
    )
    if asked != INPUT_ORIGINAL:
        return recorded
    backup = originals_dir(project_dir) / (job.get("reference") or job["image"])
    return backup if backup.is_file() else recorded


def candidate_path(job_dir: Path, index: int) -> Path:
    return Path(job_dir) / f"cand_{index:02d}.png"


def existing_candidates(job_dir: Path) -> list[int]:
    found: list[int] = []
    for path in sorted(Path(job_dir).glob("cand_*.png")):
        match = re.fullmatch(r"cand_(\d+)\.png", path.name)
        if match:
            found.append(int(match.group(1)))
    return found


def _input_changed(job_dir: Path, job: dict[str, Any]) -> bool:
    """Whether ``job`` would start from a different picture than last time.

    The same prompt over a different input is a different job: after a
    ``backfill`` the original is the larger scan, and a candidate drawn from the
    thumbnail it replaced must not be kept as if it answered the new one.
    """
    try:
        previous = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(previous, dict):
        return False
    return any(
        previous.get(field) != job.get(field)
        for field in ("input_from", "reference", "width", "height")
    )


def _archive_candidates(job_dir: Path) -> int:
    """Move a job's candidates aside because its prompt or its input changed.
    Never deletes: each one cost minutes of plan usage and may still be the one
    wanted."""
    # A composite's description goes with it: left behind, it would describe
    # whatever picture next took that number.
    moved = (
        [p for p in job_dir.glob("cand_*.png")]
        + [p for p in job_dir.glob("cand_*.composite.json")]
        + [p for p in job_dir.glob("run_*") if p.is_dir()]
    )
    if not moved:
        return 0
    target = job_dir / "previous" / time.strftime("%Y%m%d_%H%M%S")
    suffix = 0
    while target.exists():
        suffix += 1
        target = target.with_name(f"{target.name.split('-')[0]}-{suffix}")
    target.mkdir(parents=True)
    old_prompt = job_dir / "prompt.txt"
    if old_prompt.exists():
        shutil.copy2(old_prompt, target / "prompt.txt")
    for path in moved:
        shutil.move(str(path), str(target / path.name))
    return sum(1 for p in moved if p.suffix == ".png")


def prepare(project_dir: Path, jobs: list[Any], *, replace: bool = False) -> dict[str, Any]:
    """Validate ``jobs`` and render one prompt each. Spends nothing.

    All-or-nothing: any invalid job refuses the batch and writes nothing. Jobs
    are merged into the manifest by image, so re-preparing one image (a redo
    with a new note) leaves the others alone; ``replace=True`` makes the
    manifest exactly this batch instead.
    """
    project_dir = Path(project_dir)
    if not jobs:
        return {"status": "error", "error": "no jobs given"}
    try:
        # A `generate` in flight reads from the run_NN/ this archives, and saves
        # what it started under whatever prompt.txt is there when it ends.
        with locks.image_lock(project_dir, kind="image-prepare"):
            return _prepare(project_dir, jobs, replace=replace)
    except locks.LockBusy as busy:
        return {
            "status": "error",
            "error": f"an image run holds this book; nothing was prepared. {busy}",
            "counts": {"requested": len(jobs), "invalid": 0, "prepared": 0},
            "instructions": (
                "Wait for `generate` to finish, then re-run prepare with the same batch."
            ),
        }


def _prepare(project_dir: Path, jobs: list[Any], *, replace: bool) -> dict[str, Any]:
    path = manifest_path(project_dir)
    manifest = _read_manifest(path) if path.exists() else {"version": 1, "jobs": []}
    if replace:
        manifest = {"version": 1, "jobs": []}
    elif manifest is None:
        return {
            "status": "error",
            "error": (
                f"{path} is there but cannot be read; nothing was prepared. Merged "
                "onto it, this batch would be the whole manifest and every other "
                "job's candidates would leave the board."
            ),
            "counts": {"requested": len(jobs), "invalid": 0, "prepared": 0},
            "instructions": (
                "Restore the file, or re-run prepare with --replace and every job "
                "the book should keep: each job's last form is in "
                "jobs/<id>/job.json beside it."
            ),
        }
    owners = {job["id"]: job.get("image") for job in manifest["jobs"]}

    state = ledger.current_state(project_dir)
    cover = find_cover(project_dir)
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for position, raw in enumerate(jobs):
        job, problems = _validate_job(project_dir, raw, state, cover)
        if job is not None and job["id"] in seen:
            problems = [f"{job['image']}: named twice in this batch (also job #{seen[job['id']]})"]
            job = None
        elif job is not None and owners.get(job["id"], job["image"]) != job["image"]:
            # job_id_for folds every character a folder name cannot hold into
            # "_", so a/b.jpg and a_b.jpg would share one folder and one entry.
            problems = [
                f"{job['image']}: its job id {job['id']!r} already belongs to "
                f"{owners[job['id']]}; the two cannot both be prepared"
            ]
            job = None
        if job is None:
            invalid.append({
                "index": position,
                "image": raw.get("image") if isinstance(raw, dict) else None,
                "problems": problems,
            })
            continue
        seen[job["id"]] = position
        valid.append(job)

    if invalid:
        return {
            "status": "error",
            "error": f"{len(invalid)} of {len(jobs)} job(s) are invalid; nothing was prepared",
            "invalid": invalid,
            "counts": {"requested": len(jobs), "invalid": len(invalid), "prepared": 0},
            "instructions": "Fix the named problems and re-run prepare with the whole batch.",
        }

    meta = _book_meta(project_dir)
    try:
        prompts = {job["id"]: render_prompt(job, meta) for job in valid}
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": f"could not render a prompt: {exc}"}

    prepared: list[dict[str, Any]] = []
    archived_total = 0
    for job in valid:
        job_dir = jobs_dir(project_dir) / job["id"]
        job_dir.mkdir(parents=True, exist_ok=True)
        prompt = prompts[job["id"]]
        prompt_file = job_dir / "prompt.txt"
        archived = 0
        if (
            prompt_file.exists() and prompt_file.read_text(encoding="utf-8") != prompt
        ) or _input_changed(job_dir, job):
            archived = _archive_candidates(job_dir)
            archived_total += archived
        _write_atomic(prompt_file, prompt)
        job["job_dir"] = str(job_dir)
        job["prompt_path"] = str(prompt_file)
        job["prompt_sha"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        job["prepared_at"] = ledger.now_stamp()
        _write_atomic(job_dir / "job.json", json.dumps(job, ensure_ascii=False, indent=2))
        have = existing_candidates(job_dir)
        prepared.append({
            "id": job["id"],
            "image": job["image"],
            "mode": job["mode"],
            "candidates": job["candidates"],
            "model": job["model"],
            "have": len([n for n in have if n <= job["candidates"]]),
            "archived": archived,
            "input_from": job["input_from"],
            "prompt_path": job["prompt_path"],
        })

    merged = {job["id"]: job for job in manifest["jobs"]}
    merged.update({job["id"]: job for job in valid})
    manifest = {"version": 1, "updated": ledger.now_stamp(), "jobs": list(merged.values())}
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, json.dumps(manifest, ensure_ascii=False, indent=2))

    to_generate = sum(max(0, row["candidates"] - row["have"]) for row in prepared)
    return {
        "status": "ok",
        "prepared": prepared,
        "counts": {
            "requested": len(jobs),
            "prepared": len(prepared),
            "invalid": 0,
            "candidates_to_generate": to_generate,
            "archived_candidates": archived_total,
            "jobs_in_manifest": len(manifest["jobs"]),
        },
        "manifest_path": str(path),
        "instructions": (
            "Nothing has been spent. Run `generate --estimate` and get consent "
            "for the time and the plan-limit burn before `generate`."
        ),
    }


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def _usage_rows(project_dir: Path) -> list[dict[str, Any]]:
    path = usage_path(project_dir)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _measured_minutes(project_dir: Path) -> tuple[Optional[float], Optional[float]]:
    """``(median, slow)`` wall minutes of this project's successful runs, if any.

    ``slow`` is the ninth decile: what a batch run in parallel waits for is its
    slowest image, and a map with thirty labels takes three times the median.
    The very slowest is left out because one run in thirteen took 481 s for no
    visible reason (stormy-misty-s-foal, 2026-10-06).
    """
    walls = sorted(
        float(row["wall_s"])
        for row in _usage_rows(project_dir)
        if row.get("ok") and isinstance(row.get("wall_s"), (int, float)) and row["wall_s"] > 0
    )
    if not walls:
        return None, None
    slow = walls[math.ceil(0.9 * len(walls)) - 1]
    return round(statistics.median(walls) / 60.0, 1), round(slow / 60.0, 1)


def candidate_models(project_dir: Path) -> dict[tuple[str, int], str]:
    """Which model made each candidate, by ``(job id, candidate number)``.

    Read from the usage log: the newest successful run of a slot is the file
    in it. Runs logged before the model was recorded are simply absent.
    """
    made: dict[tuple[str, int], str] = {}
    for row in _usage_rows(project_dir):
        if not row.get("ok") or not isinstance(row.get("candidate"), int):
            continue
        slot = (str(row.get("id")), row["candidate"])
        if isinstance(row.get("model"), str) and row["model"]:
            made[slot] = row["model"]
        else:
            made.pop(slot, None)
    return made


def _select_jobs(
    manifest: dict[str, Any], target_ids: Optional[list[str]]
) -> tuple[list[dict[str, Any]], list[str]]:
    jobs = manifest["jobs"]
    if not target_ids:
        return jobs, []
    by_id = {job["id"]: job for job in jobs}
    by_image = {job["image"]: job for job in jobs}
    chosen: list[dict[str, Any]] = []
    unknown: list[str] = []
    for target in target_ids:
        job = by_id.get(target) or by_image.get(image_key(target))
        if job is None:
            unknown.append(target)
        elif job not in chosen:
            chosen.append(job)
    return chosen, unknown


def _todo(jobs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """Every ``(job, candidate index)`` that has no file yet."""
    out: list[tuple[dict[str, Any], int]] = []
    for job in jobs:
        have = set(existing_candidates(Path(job["job_dir"])))
        out.extend((job, n) for n in range(1, job["candidates"] + 1) if n not in have)
    return out


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _is_image_path(value: str) -> bool:
    return value.lower().endswith(IMAGE_SUFFIXES)


def _event_image_paths(stdout: str, run_dir: Path) -> tuple[list[Path], list[Path]]:
    """Image files the ``--json`` event stream names: ``(saved_path, others)``.

    The built-in tool reports where it wrote under a ``saved_path`` key; any
    other string that is an existing image file is a weaker second guess.
    """
    saved: list[Path] = []
    others: list[Path] = []

    def visit(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for name, value in node.items():
                visit(value, str(name))
        elif isinstance(node, list):
            for value in node:
                visit(value, key)
        elif isinstance(node, str) and len(node) < 1024 and _is_image_path(node):
            path = Path(node)
            if not path.is_absolute():
                path = run_dir / path
            if path.is_file():
                (saved if key == "saved_path" else others).append(path)

    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            visit(json.loads(line))
        except json.JSONDecodeError:
            continue
    return saved, others


def thread_id_of(stdout: str) -> Optional[str]:
    """The session id from the stream's ``{"type":"thread.started",…}`` event."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and re.fullmatch(r"[A-Za-z0-9._-]+", thread_id):
                return thread_id
    return None


def harvest(
    run_dir: Path,
    *,
    stdout: str,
    input_copy: Optional[Path],
) -> tuple[Optional[Path], Optional[str], int]:
    """Find the image a Codex run produced. Returns ``(path, where, generated)``.

    ``generated`` is how many images the run made when that is knowable (the
    thread folder), else 0. More than one means the model ignored "call the
    tool once" and the job cost that many times the plan usage.

    Most trustworthy first:

    1. ``$CODEX_HOME/generated_images/<thread id>/`` — where the built-in tool
       writes (``exec-<uuid>.png``), keyed by the session id the event stream
       opens with. Exact attribution, so it is safe at any concurrency. The
       newest file wins: it is the model's last word.
    2. an image in the run dir — a model that saved one there itself.
    3. a ``saved_path`` or any other image path the event stream named. Not
       seen on 0.157.0, whose ``--json`` stream carries no image events at all;
       kept for a CLI that starts reporting them.

    A file that merely appeared under ``$CODEX_HOME/generated_images`` during
    the run is not taken. It used to be, when one job ran at a time; but any
    other Codex on the machine writes there too (a second book's ``generate``,
    the user's own session), and another session's picture in this job's slot
    is worse than a run reported as having left no image. The thread folder
    held on all 71 live runs to 2026-10-06.
    """
    skip = {input_copy.resolve()} if input_copy is not None else set()

    def usable(path: Path) -> bool:
        try:
            return path.is_file() and path.resolve() not in skip and path.stat().st_size > 0
        except OSError:
            return False

    def newest(paths: list[Path]) -> Path:
        return max(paths, key=lambda p: p.stat().st_mtime)

    root = codex_home() / "generated_images"
    thread_id = thread_id_of(stdout)
    if thread_id:
        thread_dir = root / thread_id
        made = [
            p for p in (thread_dir.rglob("*") if thread_dir.is_dir() else [])
            if p.suffix.lower() in IMAGE_SUFFIXES and usable(p)
        ]
        if made:
            return newest(made), "codex_home:thread", len(made)

    in_dir = [
        p for p in run_dir.rglob("*")
        if p.suffix.lower() in IMAGE_SUFFIXES and usable(p)
    ]
    if in_dir:
        contract = run_dir / OUTPUT_NAME
        return (contract if contract in in_dir else newest(in_dir)), "run_dir", 0

    saved, others = _event_image_paths(stdout, run_dir)
    for where, paths in (("event:saved_path", saved), ("event:path", others)):
        for path in reversed(paths):
            if usable(path):
                return path, where, 0
    return None, None, 0


def _save_candidate(source: Path, target: Path, input_copy: Optional[Path]) -> dict[str, Any]:
    """Store ``source`` as a PNG candidate; returns its dimensions or an error."""
    try:
        from PIL import Image
    except ImportError:
        return {"error": "Pillow is not installed — pip install -r requirements.txt"}
    if input_copy is not None and source.read_bytes() == input_copy.read_bytes():
        return {"error": "the output is byte-identical to the input: nothing was generated"}
    try:
        with Image.open(source) as image:
            image.load()
            width, height = image.size
            tmp = target.with_suffix(".tmp.png")
            if image.format == "PNG":
                shutil.copyfile(source, tmp)
            else:
                image.save(tmp, format="PNG")
        os.replace(tmp, target)
    except Exception as exc:  # noqa: BLE001 - a non-image "output" is one outcome
        return {"error": f"the harvested file is not a readable image: {exc}"}
    return {"width": width, "height": height}


_USAGE_LIMIT_RE = re.compile(r"usage[_ ]limit|rate[_ ]limit|quota", re.IGNORECASE)
# The model in ~/.codex/config.toml can be one a ChatGPT login may not use
# (observed 2026-10-05 with `model = "gpt-5.4"`). Every job would fail the same
# way in ten seconds, so the first one stops the batch.
_MODEL_REJECTED_RE = re.compile(r"model is not supported|model .* does not exist", re.IGNORECASE)


def generate(
    project_dir: Path,
    *,
    estimate: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    target_ids: Optional[list[str]] = None,
    limit: Optional[int] = None,
    cli_bin: Optional[str] = None,
    model: str | Sequence[str] | None = None,
    timeout_s: Optional[float] = None,
    runner: Optional[headless.Runner] = None,
    prober: Optional[headless.AuthProber] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Run Codex once per missing candidate and harvest each image.

    Subscription-only and fail-closed: the login probe runs before the estimate
    is reported and again before every job, and a refusal stops the batch with
    nothing spent. A stub ``runner`` with no ``prober`` skips the probe, which
    is what lets the tests run without a Codex install.

    Each candidate runs on the first of: its job's ``model``, ``model`` here,
    the book's pinned ``image_model``, Codex's own default. Where that names
    several, candidate ``n`` takes the ``n``-th, round again — so two models on
    one job are two candidates of it, in this one process. ``limit`` runs only
    the first so many missing candidates; ``progress`` is told as each finishes.
    """
    project_dir = Path(project_dir)
    if concurrency < 1:
        return {"status": "error", "error": f"invalid concurrency {concurrency!r}; must be >= 1"}
    if limit is not None and limit < 1:
        return {"status": "error", "error": f"invalid limit {limit!r}; must be >= 1"}
    asked_models, model_problem = split_models(model)
    if model_problem:
        return {"status": "error", "error": f"--model: {model_problem}"}
    pinned, pinned_problem = pinned_models(project_dir)
    if pinned_problem and not asked_models:
        # Run on Codex's default instead, the batch would be spent on a model
        # nobody chose.
        return {
            "status": "error",
            "error": (
                f"this book's pinned {MODEL_CONFIG_KEY}: {pinned_problem}. Set it "
                f"again with `harness.py config-set --key {MODEL_CONFIG_KEY} "
                "--value <id>`, or name a model with --model."
            ),
        }
    fallback_models = asked_models or pinned or [None]

    def model_of(job: dict[str, Any], index: int) -> Optional[str]:
        models = job.get("model") or fallback_models
        return models[(index - 1) % len(models)]

    manifest = load_manifest(project_dir)
    if not manifest["jobs"]:
        return {"status": "error", "error": "no prepared jobs — run `prepare` first"}
    jobs, unknown = _select_jobs(manifest, target_ids)
    if unknown:
        return {
            "status": "error",
            "error": f"--target-ids names job(s) that are not prepared: {unknown}",
            "prepared_ids": [job["id"] for job in manifest["jobs"]],
        }

    owed = _todo(jobs)
    todo = owed[:limit] if limit else owed
    minutes, slow = _measured_minutes(project_dir)
    per_image = minutes if minutes is not None else ASSUMED_MINUTES_PER_IMAGE
    slowest = slow if slow is not None else ASSUMED_MINUTES_PER_IMAGE
    # More workers than candidates are workers with nothing to do.
    workers = max(1, min(concurrency, len(todo)))
    models: dict[str, int] = {}
    for job, n in todo:
        label = model_of(job, n) or DEFAULT_MODEL_LABEL
        models[label] = models.get(label, 0) + 1
    plan = {
        "cli": headless.IMAGE_CLI,
        "models": models,
        "images": len({job["id"] for job, _n in todo}),
        "candidates": len(todo),
        "already_have": sum(job["candidates"] for job in jobs) - len(owed),
        "held_back": len(owed) - len(todo),
        "concurrency": concurrency,
        "workers": workers,
        "minutes_per_image": per_image,
        "minutes_slowest": slowest,
        "minutes_measured": minutes is not None,
        "sequential_minutes": round(len(todo) * per_image, 1),
        # In parallel a batch lasts as long as its slowest image, however few
        # there are: two candidates across ten workers still take one image's
        # minutes, not a fifth of them.
        "estimated_minutes": (
            round(max(len(todo) * per_image / workers, slowest), 1) if todo else 0.0
        ),
        "jobs": [
            {
                "id": job["id"],
                "mode": job["mode"],
                "missing": [n for j, n in todo if j is job],
                "models": [
                    model_of(job, n) or DEFAULT_MODEL_LABEL for j, n in todo if j is job
                ],
            }
            for job in jobs
            if any(j is job for j, _n in todo)
        ],
    }

    if not todo:
        return {
            "status": "ok",
            "estimate": estimate,
            "plan": plan,
            "wrote": [],
            "failed": [],
            "counts": {"wrote": 0, "failed": 0, "not_run": 0, "todo": 0},
            "instructions": (
                "Every prepared candidate already exists. To roll another, raise "
                "`candidates` on the job and re-run prepare; to change the "
                "result, change the instruction."
            ),
        }

    # Hoisted ahead of the estimate for the reason preflight_error gives: an
    # estimate that goes green on a logged-out or API-key Codex gets consent
    # for a run that will then refuse.
    if runner is None or prober is not None:
        preflight = headless.image_preflight_error(cli_bin, prober=prober)
        if preflight:
            return {
                "status": "error",
                "error": preflight,
                "estimate": estimate,
                "plan": plan,
                "wrote": [],
                "failed": [],
                "counts": {"wrote": 0, "failed": 0, "not_run": len(todo), "todo": len(todo)},
                "instructions": (
                    "Nothing ran and nothing was spent. Image jobs are "
                    "subscription-only; there is no flag to override this."
                ),
            }

    if estimate:
        return {
            "status": "ok",
            "estimate": True,
            "plan": plan,
            "limit_warning": LIMIT_WARNING,
            "instructions": (
                "Quote plan and limit_warning to the user verbatim and get "
                "consent in a separate turn before running `generate`. "
                "estimated_minutes is with plan.workers running at once; "
                "sequential_minutes is the same work one at a time. The usage "
                "spent is the same either way."
                + (
                    ""
                    if minutes is not None
                    else " minutes_per_image is an assumption, not a measurement: "
                    "say so."
                )
            ),
        }

    abort = threading.Event()
    abort_reason: list[str] = []
    # Models the plan turned down, by id ("" for Codex's default). Only their
    # own candidates stop: in a batch on two models the other one's still land.
    rejected: set[str] = set()
    state_lock = threading.Lock()

    def log(row: dict[str, Any]) -> None:
        with state_lock:
            path = usage_path(project_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def run_one(job: dict[str, Any], index: int) -> dict[str, Any]:
        run_model = model_of(job, index)
        base = {
            "id": job["id"],
            "image": job["image"],
            "candidate": index,
            "mode": job["mode"],
            "model": run_model,
        }
        if abort.is_set():
            return {**base, "ok": False, "skipped": True, "error": "not run: batch stopped"}
        if (run_model or "") in rejected:
            return {
                **base, "ok": False, "skipped": True,
                "error": "not run: this model was rejected",
            }
        job_dir = Path(job["job_dir"])
        run_dir = job_dir / f"run_{index:02d}"
        if run_dir.exists():
            shutil.rmtree(run_dir)  # our own scratch from a failed attempt
        run_dir.mkdir(parents=True)
        input_copy: Optional[Path] = None
        if job.get("input"):
            source = _input_path(project_dir, job)
            if not source.is_file():
                return {**base, "ok": False, "error": f"input image is gone: {source}"}
            input_copy = run_dir / job["input_name"]
            shutil.copyfile(source, input_copy)
        prompt = Path(job["prompt_path"]).read_text(encoding="utf-8")
        result = headless.run_image_job(
            prompt,
            job_dir=run_dir,
            images=[input_copy] if input_copy is not None else [],
            cli_bin=cli_bin,
            model=run_model,
            timeout=timeout_s,
            runner=runner,
            prober=prober,
        )
        try:
            (run_dir / "events.jsonl").write_text(result["stdout"] or "", encoding="utf-8")
            if result["stderr"]:
                (run_dir / "stderr.txt").write_text(result["stderr"], encoding="utf-8")
        except OSError:
            pass
        row = {**base, "ok": False, "rc": result["rc"], "wall_s": result["wall_s"]}
        if not result["ok"]:
            row["error"] = result["error"]
            if result.get("preflight_failed"):
                abort_reason.append(result["error"])
                abort.set()
            elif _MODEL_REJECTED_RE.search(result["error"] or ""):
                with state_lock:
                    # Said once per model: at ten workers, ten runs on it are
                    # already in flight and each comes back with the same line.
                    if (run_model or "") not in rejected:
                        rejected.add(run_model or "")
                        abort_reason.append(
                            f"{result['error']} Pass `--model <id>` with a model "
                            "this ChatGPT plan offers (the ids are in "
                            "~/.codex/models_cache.json); nothing was generated "
                            "on it."
                        )
            elif _USAGE_LIMIT_RE.search(result["error"] or ""):
                with state_lock:
                    if not abort.is_set():
                        abort_reason.append(
                            "Codex reported a usage limit; stopped so the rest of "
                            f"the batch is not burned against it ({result['error']})"
                        )
                    abort.set()
            return row
        found, where, generated = harvest(
            run_dir,
            stdout=result["stdout"],
            input_copy=input_copy,
        )
        if found is None:
            row["error"] = (
                "codex exited 0 but left no image: looked in "
                "$CODEX_HOME/generated_images/<thread id>/, in the run dir and "
                f"in the event stream. Read {run_dir / 'events.jsonl'}"
            )
            return row
        target = candidate_path(job_dir, index)
        saved = _save_candidate(found, target, input_copy)
        if saved.get("error"):
            row["error"] = saved["error"]
            return row
        row.update(ok=True, path=str(target), harvested_from=where, **saved)
        if generated > 1:
            # The model called the tool more than once despite the contract:
            # this candidate cost that many images of plan usage.
            row["images_generated"] = generated
        return row

    def attempt(job: dict[str, Any], index: int) -> dict[str, Any]:
        try:
            return run_one(job, index)
        except Exception as exc:  # noqa: BLE001 - one candidate's failure, not the batch's
            # Raised out of the pool it would end the run with a traceback while
            # the other workers went on unlogged.
            return {
                "id": job["id"],
                "image": job["image"],
                "candidate": index,
                "mode": job["mode"],
                "model": model_of(job, index),
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    rows: list[dict[str, Any]] = []

    def finished(row: dict[str, Any]) -> None:
        rows.append(row)
        if not row.get("skipped"):
            log({"ts": ledger.now_stamp(), **row, "concurrency": workers})
        if progress is not None:
            outcome = (
                "not run" if row.get("skipped")
                else f"ok {row['wall_s']:.0f}s" if row.get("ok")
                else f"failed: {str(row.get('error') or '')[:120]}"
            )
            progress(
                f"[{len(rows)}/{len(todo)}] {row['image']} #{row['candidate']} "
                f"{outcome} ({row.get('model') or 'default model'})"
            )

    def run_batch() -> None:
        if workers == 1:
            for job, index in todo:
                finished(attempt(job, index))
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(attempt, job, index) for job, index in todo]
            try:
                for future in as_completed(futures):
                    finished(future.result())
            except KeyboardInterrupt:
                # Ctrl-C reaches only this thread; the workers are blocked in
                # communicate() and have to be killed from here.
                abort.set()
                headless.kill_live_processes()
                raise

    began = time.monotonic()
    try:
        # Two `generate` processes on one book would each run every missing
        # candidate, spend double, and clear each other's run_NN/ as scratch.
        with locks.image_lock(project_dir):
            # Counted before the lock was ours: a run that ended in between has
            # made some of these, and running them again would spend twice.
            still = {(job["id"], n) for job, n in _todo(jobs)}
            todo = [(job, n) for job, n in todo if (job["id"], n) in still]
            run_batch()
    except locks.LockBusy as busy:
        return {
            "status": "error",
            "error": (
                "another image run (`generate` or `prepare`) is already running "
                f"on this book. {busy}"
            ),
            "estimate": False,
            "plan": plan,
            "wrote": [],
            "failed": [],
            "counts": {"wrote": 0, "failed": 0, "not_run": len(todo), "todo": len(todo)},
            "instructions": (
                "Nothing ran and nothing was spent. Wait for that run to finish, "
                "then run `generate` again: it fills only what is still missing."
            ),
        }

    wrote = [row for row in rows if row.get("ok")]
    skipped = [row for row in rows if row.get("skipped")]
    failed = [row for row in rows if not row.get("ok") and not row.get("skipped")]
    out: dict[str, Any] = {
        "status": "ok" if not failed and not skipped else "partial" if wrote else "error",
        "estimate": False,
        "plan": plan,
        "wrote": sorted(wrote, key=lambda r: (r["id"], r["candidate"])),
        "failed": sorted(failed, key=lambda r: (r["id"], r["candidate"])),
        "counts": {
            "wrote": len(wrote),
            "failed": len(failed),
            "not_run": len(skipped),
            "todo": len(todo),
        },
        # Side by side these say what running in parallel bought: the batch
        # took elapsed_s, and would have taken wall_s_summed one at a time.
        "elapsed_s": round(time.monotonic() - began, 1),
        "wall_s_summed": round(
            sum(row.get("wall_s") or 0.0 for row in rows if not row.get("skipped")), 1
        ),
        "usage_log": str(usage_path(project_dir)),
    }
    if abort_reason:
        # A refused login comes back from every run already in flight.
        out["error"] = " ".join(dict.fromkeys(abort_reason))
    out["instructions"] = (
        "Run `board`, Read each candidate beside its original, and record what "
        "you find with `check` before the pick gate."
        if wrote
        else "Another run made every candidate this one set out to; nothing ran "
        "and nothing was spent."
        if not rows
        else "No candidate was produced. Relay the error(s) verbatim."
    ) + (
        " Re-running `generate` retries only what is still missing — never "
        "re-prepare to recover."
        if failed or skipped
        else ""
    )
    return out
