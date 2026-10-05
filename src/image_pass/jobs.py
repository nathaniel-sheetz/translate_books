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
    jobs/<id>/cand_NN.png         harvested candidates, 1-based
    jobs/<id>/run_NN/             that candidate's Codex working directory: the
                                  input copy, events.jsonl, stderr.txt
    jobs/<id>/previous/<stamp>/   candidates of an earlier prompt, never deleted
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from src.harness import headless
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

INPUT_ORIGINAL = "original"
INPUT_CURRENT = "current"

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

_JOB_KEYS = frozenset({"image", "mode", "instruction", "labels", "candidates", "input", "note"})

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


def load_manifest(project_dir: Path) -> dict[str, Any]:
    path = manifest_path(project_dir)
    if not path.exists():
        return {"version": 1, "jobs": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "jobs": []}
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), list):
        return {"version": 1, "jobs": []}
    doc["jobs"] = [job for job in doc["jobs"] if isinstance(job, dict) and job.get("id")]
    return doc


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


def _render_labels(labels: dict[str, str]) -> str:
    return "\n".join(f'- "{source}" → "{target}"' for source, target in labels.items())


def load_template(mode: str) -> str:
    return (PROMPTS_DIR / f"{mode}.txt").read_text(encoding="utf-8")


def render_prompt(job: dict[str, Any], meta: dict[str, str]) -> str:
    """Fill ``prompts/image_pass/<mode>.txt`` for one validated job."""
    labels = job.get("labels") or {}
    input_name = job.get("input_name")
    if input_name:
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
    elif current.is_file():
        source = current

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
        "note": raw.get("note") if isinstance(raw.get("note"), str) else None,
        "input": str(source) if source is not None else None,
        "input_from": (
            None if source is None
            else INPUT_ORIGINAL if source == backup or not backup.is_file()
            else INPUT_CURRENT
        ),
        "input_name": f"input{source.suffix.lower()}" if source is not None else None,
        "width": width,
        "height": height,
        "status_at_prepare": ledger.status_of(state.get(key)),
    }, []


def candidate_path(job_dir: Path, index: int) -> Path:
    return Path(job_dir) / f"cand_{index:02d}.png"


def existing_candidates(job_dir: Path) -> list[int]:
    found: list[int] = []
    for path in sorted(Path(job_dir).glob("cand_*.png")):
        match = re.fullmatch(r"cand_(\d+)\.png", path.name)
        if match:
            found.append(int(match.group(1)))
    return found


def _archive_candidates(job_dir: Path) -> int:
    """Move a job's candidates aside because its prompt changed. Never deletes:
    each one cost minutes of plan usage and may still be the one wanted."""
    moved = [p for p in job_dir.glob("cand_*.png")] + [
        p for p in job_dir.glob("run_*") if p.is_dir()
    ]
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
        if prompt_file.exists() and prompt_file.read_text(encoding="utf-8") != prompt:
            archived = _archive_candidates(job_dir)
            archived_total += archived
        prompt_file.write_text(prompt, encoding="utf-8")
        job["job_dir"] = str(job_dir)
        job["prompt_path"] = str(prompt_file)
        job["prompt_sha"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        job["prepared_at"] = ledger.now_stamp()
        (job_dir / "job.json").write_text(
            json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        have = existing_candidates(job_dir)
        prepared.append({
            "id": job["id"],
            "image": job["image"],
            "mode": job["mode"],
            "candidates": job["candidates"],
            "have": len([n for n in have if n <= job["candidates"]]),
            "archived": archived,
            "input_from": job["input_from"],
            "prompt_path": job["prompt_path"],
        })

    manifest = {"version": 1, "jobs": []} if replace else load_manifest(project_dir)
    merged = {job["id"]: job for job in manifest["jobs"]}
    merged.update({job["id"]: job for job in valid})
    manifest = {"version": 1, "updated": ledger.now_stamp(), "jobs": list(merged.values())}
    path = manifest_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

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

def _measured_minutes(project_dir: Path) -> Optional[float]:
    """Median wall minutes of this project's successful runs, if any."""
    path = usage_path(project_dir)
    if not path.exists():
        return None
    walls: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("ok") and isinstance(row.get("wall_s"), (int, float)):
            if row["wall_s"] > 0:
                walls.append(float(row["wall_s"]))
    if not walls:
        return None
    return round(statistics.median(walls) / 60.0, 1)


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


def _generated_images_snapshot() -> set[str]:
    root = codex_home() / "generated_images"
    if not root.is_dir():
        return set()
    return {str(p) for p in root.rglob("*") if p.is_file()}


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
    home_before: Optional[set[str]],
    started: float,
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
    4. a file that appeared anywhere under ``$CODEX_HOME/generated_images``
       during the run — only when ``home_before`` is given, i.e. when no other
       Codex job was running that could have written it.
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

    if home_before is not None:
        fresh = [
            p for p in (root.rglob("*") if root.is_dir() else [])
            if p.is_file()
            and p.suffix.lower() in IMAGE_SUFFIXES
            and str(p) not in home_before
            and p.stat().st_mtime >= started - 2
        ]
        if fresh:
            return newest(fresh), "codex_home:new_file", len(fresh)
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
    concurrency: int = 1,
    target_ids: Optional[list[str]] = None,
    cli_bin: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: Optional[float] = None,
    runner: Optional[headless.Runner] = None,
    prober: Optional[headless.AuthProber] = None,
) -> dict[str, Any]:
    """Run Codex once per missing candidate and harvest each image.

    Subscription-only and fail-closed: the login probe runs before the estimate
    is reported and again before every job, and a refusal stops the batch with
    nothing spent. A stub ``runner`` with no ``prober`` skips the probe, which
    is what lets the tests run without a Codex install.
    """
    project_dir = Path(project_dir)
    if concurrency < 1:
        return {"status": "error", "error": f"invalid concurrency {concurrency!r}; must be >= 1"}
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

    todo = _todo(jobs)
    minutes = _measured_minutes(project_dir)
    per_image = minutes if minutes is not None else ASSUMED_MINUTES_PER_IMAGE
    plan = {
        "cli": headless.IMAGE_CLI,
        "model": model or "(Codex default, from ~/.codex/config.toml)",
        "images": len({job["id"] for job, _n in todo}),
        "candidates": len(todo),
        "already_have": sum(job["candidates"] for job in jobs) - len(todo),
        "concurrency": concurrency,
        "minutes_per_image": per_image,
        "minutes_measured": minutes is not None,
        "estimated_minutes": round(len(todo) * per_image / concurrency, 1),
        "jobs": [
            {
                "id": job["id"],
                "mode": job["mode"],
                "missing": [n for j, n in todo if j is job],
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
            "counts": {"wrote": 0, "failed": 0, "todo": 0},
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
                "counts": {"wrote": 0, "failed": 0, "todo": len(todo)},
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
                "consent in a separate turn before running `generate`."
                + (
                    ""
                    if minutes is not None
                    else " minutes_per_image is an assumption, not a measurement: "
                    "say so."
                )
            ),
        }

    # A file under $CODEX_HOME can only be attributed to a job when no other
    # job could have written it.
    attribute_home = concurrency == 1
    abort = threading.Event()
    abort_reason: list[str] = []
    log_lock = threading.Lock()

    def log(row: dict[str, Any]) -> None:
        with log_lock:
            path = usage_path(project_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def run_one(job: dict[str, Any], index: int) -> dict[str, Any]:
        base = {"id": job["id"], "image": job["image"], "candidate": index}
        if abort.is_set():
            return {**base, "ok": False, "skipped": True, "error": "not run: batch stopped"}
        job_dir = Path(job["job_dir"])
        run_dir = job_dir / f"run_{index:02d}"
        if run_dir.exists():
            shutil.rmtree(run_dir)  # our own scratch from a failed attempt
        run_dir.mkdir(parents=True)
        input_copy: Optional[Path] = None
        if job.get("input"):
            source = Path(job["input"])
            if not source.is_file():
                return {**base, "ok": False, "error": f"input image is gone: {source}"}
            input_copy = run_dir / job["input_name"]
            shutil.copyfile(source, input_copy)
        prompt = Path(job["prompt_path"]).read_text(encoding="utf-8")
        home_before = _generated_images_snapshot() if attribute_home else None
        started = time.time()
        result = headless.run_image_job(
            prompt,
            job_dir=run_dir,
            images=[input_copy] if input_copy is not None else [],
            cli_bin=cli_bin,
            model=model,
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
                abort_reason.append(
                    f"{result['error']} Pass `--model <id>` with a model this "
                    "ChatGPT plan offers (the ids are in "
                    "~/.codex/models_cache.json); nothing was generated."
                )
                abort.set()
            elif _USAGE_LIMIT_RE.search(result["error"] or ""):
                abort_reason.append(
                    "Codex reported a usage limit; stopped so the rest of the "
                    f"batch is not burned against it ({result['error']})"
                )
                abort.set()
            return row
        found, where, generated = harvest(
            run_dir,
            stdout=result["stdout"],
            input_copy=input_copy,
            home_before=home_before,
            started=started,
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

    rows: list[dict[str, Any]] = []
    if concurrency == 1:
        for job, index in todo:
            row = run_one(job, index)
            rows.append(row)
            if not row.get("skipped"):
                log({"ts": ledger.now_stamp(), **row})
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(run_one, job, index) for job, index in todo]
            try:
                for future in as_completed(futures):
                    row = future.result()
                    rows.append(row)
                    if not row.get("skipped"):
                        log({"ts": ledger.now_stamp(), **row})
            except KeyboardInterrupt:
                # Ctrl-C reaches only this thread; the workers are blocked in
                # communicate() and have to be killed from here.
                abort.set()
                headless.kill_live_processes()
                raise

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
        "usage_log": str(usage_path(project_dir)),
    }
    if abort_reason:
        out["error"] = abort_reason[0]
    out["instructions"] = (
        "Run `review`, then Read each candidate beside its original before the "
        "pick gate."
        if wrote
        else "No candidate was produced. Relay the error(s) verbatim."
    ) + (
        " Re-running `generate` retries only what is still missing — never "
        "re-prepare to recover."
        if failed or skipped
        else ""
    )
    return out
