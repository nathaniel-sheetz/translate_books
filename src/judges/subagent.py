"""
Subagent backend for tailored judges: render prompts, collect drafts.

The two judge backends mirror translate-harness's two translation backends:

- **API backend** (``runner.run_judge_suite`` / ``run_judges.py run``) calls the
  LLM directly, behind a dollar cost gate — metered spend, needs an API key.
- **Subagent backend** (this module / ``run_judges.py prepare`` + ``commit``)
  renders each judge's prompt to a file for a spawned ``judge-worker`` subagent,
  then collects and parses the worker's JSON verdict — **zero API spend**, runs
  on the session.

Both backends share the same seam — ``Judge.build_prompt`` (used by ``prepare``
*and* the API ``run``) and ``Judge.parse_response`` (used by ``commit`` *and* the
API ``run``) — so a judge result is byte-for-byte the same whichever backend ran,
and persists identically into ``evaluations/<chunk>.json``.

Unlike translation, judges have no cross-chunk continuity, so there is no
sequential / chapter spawn-mode machinery here — just bounded-batch parallel
spawning (the orchestrator's ``batch_size``). Files live under
``<project>/.harness/judges/``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from src.evaluators import aggregate_results
from src.judges.base import Judge, JudgeTarget
from src.judges.llm_io import JudgeParseError, parse_judge_json
from src.judges.registry import get_judge
from src.judges.runner import build_run_header, estimate_suite_cost
from src.judges.scope import _ID_RE, build_targets

logger = logging.getLogger(__name__)

_DEFAULT_WORKER_MODEL = "sonnet"
_DEFAULT_BATCH_SIZE = 5

# --- Dialogue-density gating for target grouping ----------------------------
# A target's dialogue-marker count decides whether it is judged solo or packed
# with others into one worker prompt. The raya (— U+2014) marks every spoken
# turn and inciso; the guillemets (« U+00AB, » U+00BB) mark nested speech and
# internal thoughts. Both drive the per-target reasoning a dialogue judge does,
# and the token log found raya count correlates ~0.76 with a worker's tokens —
# so this count is the signal for what is cheap enough to batch.
_DIALOGUE_MARKERS = ("—", "«", "»")  # — « »

# A target above this many markers is judged SOLO (never grouped) so its
# reasoning-heavy work is never compressed. (The token log's two priciest chunks
# had 74 and 89 rayas.) Guillemets now add to the count, so re-validate this
# threshold against a book's marker distribution before leaning on it.
_DENSITY_SOLO_THRESHOLD = 60

# A group's combined marker count stays at/under this, bounding the worker's
# output tokens so a dense batch can't truncate mid-verdict and lose members.
_GROUP_MARKER_CAP = 120


def _dialogue_marker_count(text: str) -> int:
    """Count dialogue-density markers — the raya (—) plus guillemets (« »)."""
    return sum(text.count(marker) for marker in _DIALOGUE_MARKERS)


def _group_targets(
    targets: list[JudgeTarget], judge: Judge, targets_per_worker: int
) -> list[list[JudgeTarget]]:
    """Partition targets into per-worker groups by dialogue-marker density.

    ``targets_per_worker <= 1`` (or a judge with no ``batch_template``) yields one
    target per group — today's behavior exactly. Otherwise dense targets are
    emitted solo and the rest are packed up to ``targets_per_worker`` members and
    under :data:`_GROUP_MARKER_CAP` combined markers, preserving input order.
    """
    if targets_per_worker <= 1 or not getattr(judge, "batch_template", None):
        return [[target] for target in targets]

    groups: list[list[JudgeTarget]] = []
    pack: list[JudgeTarget] = []
    pack_markers = 0

    def _flush() -> None:
        nonlocal pack, pack_markers
        if pack:
            groups.append(pack)
            pack = []
            pack_markers = 0

    for target in targets:
        markers = _dialogue_marker_count(target.translated_text or "")
        if markers > _DENSITY_SOLO_THRESHOLD:
            _flush()  # dense chunk breaks the current pack and goes solo
            groups.append([target])
            continue
        if pack and (
            len(pack) >= targets_per_worker
            or pack_markers + markers > _GROUP_MARKER_CAP
        ):
            _flush()
        pack.append(target)
        pack_markers += markers
    _flush()
    return groups


def _is_batch_entry(entry: dict[str, Any]) -> bool:
    """True if a manifest entry carries several targets in one worker prompt."""
    return isinstance(entry.get("members"), list)


def _entry_members(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize an entry to a list of members so solo and grouped entries share
    one downstream shape.

    A grouped entry carries a ``members`` list; a solo entry carries a top-level
    ``target_id`` (the pre-grouping manifest shape, still emitted for size-1
    groups and by any older manifest).
    """
    if _is_batch_entry(entry):
        return [
            m for m in entry["members"] if isinstance(m, dict) and m.get("target_id")
        ]
    if entry.get("target_id"):
        return [
            {"target_id": entry["target_id"], "target_type": entry.get("target_type", "chunk")}
        ]
    return []


def _entry_malformed_members(entry: dict[str, Any]) -> list[Any]:
    """Raw ``members`` items a batch entry carries that :func:`_entry_members`
    drops (not a dict, or no ``target_id``).

    Surfaced so the commit loop can record each as a failure instead of
    silently discarding it — a corrupt member must still be accounted for.
    """
    if not _is_batch_entry(entry):
        return []
    return [
        m for m in entry["members"] if not (isinstance(m, dict) and m.get("target_id"))
    ]


def _parse_batch_verdicts(raw: str) -> dict[str, Any]:
    """Extract the ``verdicts`` object from a batched worker draft.

    Raises :class:`JudgeParseError` if the draft is not JSON or lacks a
    ``verdicts`` object — the same error type the solo path raises, so a whole
    unparseable batch fails all its members for re-spawn.
    """
    data = parse_judge_json(raw, ("verdicts",))
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, dict):
        raise JudgeParseError(
            f"batch 'verdicts' is not an object: {type(verdicts).__name__}"
        )
    return verdicts


_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "manifest": "list of work entries (one worker each). Solo: {target_id, target_type, judge, "
    "prompt_path, draft_path, source_word_count, optional preamble_path+body_path for headless "
    "cache}. Grouped (targets-per-worker > 1): "
    "{batch_id, judge, prompt_path, draft_path, members:[{target_id, target_type, source_word_count}]} "
    "(no preamble/body — cache is solo-only)",
    "manifest_path": "path to the written manifest.json (commit reads this)",
    "scopes": "the list of --scope values resolved into this one manifest",
    "judges": "judge names rendered",
    "worker_model": "model tier to pin each spawned judge-worker to (default sonnet)",
    "batch_size": "recommended workers to spawn per wave",
    "usage_summary": "{pairs, targets, workers, targets_per_worker, source_words, worker_model, "
    "batch_size, estimated_api_cost}",
    "instructions": "what to do with the manifest (spawn workers, then commit)",
}

_FANOUT_SCHEMA = {
    "wrote": "list of entry ids whose drafts were written this wave",
    "failed": "list of {id, error} — re-run fanout for these",
    "skipped": "list of entry ids that already had a non-empty draft",
    "worker_model": "model tier used for claude -p",
    "concurrency": "max parallel claude -p processes per wave",
    "cwd": "neutral empty cwd used for the workers",
    "counts": "{wrote, failed, skipped, todo}",
    "instructions": "next step (commit, or re-fanout failed/missing)",
}

_COMMIT_SCHEMA = {
    "status": "'ok' | 'error'",
    "committed": "list of {target_id, judge} parsed this run",
    "failed": "list of {target_id, judge, problem} — re-spawn these (capped ~3x)",
    "missing": "list of {target_id, judge} whose draft file was absent — re-spawn",
    "counts": "{committed, failed, missing}",
    "summary": "aggregate_results() rollup across committed results",
    "run_header": "reproducibility metadata: judge versions, prompt hashes, backend=subagent, worker_model, git_commit",
    "results": "per committed (target, judge): serialized EvalResult",
    "persisted": "evaluations/*.json paths written when --persist is set, else null",
    "persist_errors": "list of '<target>/<judge>: <error>' strings for failed persists, else null",
}


def _judges_dir(project_dir: Path) -> Path:
    """Subagent working dir: ``<project>/.harness/judges/`` (shared .harness root)."""
    return Path(project_dir) / ".harness" / "judges"


def prepare(
    project_dir: Path,
    judge_names: list[str],
    scopes: str | list[str],
    *,
    context: Optional[dict[str, Any]] = None,
    worker_model: Optional[str] = None,
    batch_size: Optional[int] = None,
    targets_per_worker: int = 1,
    keep_drafts: bool = False,
) -> dict[str, Any]:
    """Render judge prompts plus a manifest for spawned workers (no spend).

    ``scopes`` may be a single scope string or a list of them; all are resolved
    into **one** manifest so a multi-chapter request is a single ``prepare`` ->
    spawn -> ``commit`` (no manifest clobbering). ``(target_id, judge)`` pairs are
    deduped, so overlapping scopes (e.g. ``chapter:X`` + ``chunk:X_chunk_000``)
    render once.

    ``targets_per_worker`` (default 1) enables **density-gated target grouping**:
    with the default, each ``(target, judge)`` renders its own solo prompt via
    ``judge.build_prompt`` — byte-identical to the API path — written to
    ``.harness/judges/<target_id>.<judge>.prompt.txt``. With a value > 1, several
    *low-dialogue-density* targets for a judge share one combined prompt (the rule
    block once + one ``<item>`` per target) written to ``<batch_id>.<judge>.prompt.txt``,
    amortizing per-worker overhead. Dialogue-dense targets (see
    :data:`_DENSITY_SOLO_THRESHOLD`) always stay solo. Each entry gets a
    ``draft_path`` the worker writes its JSON verdict to.

    For **solo** entries only, when the template's cache-split marker is present,
    also write a per-judge shared ``preamble.<judge>.txt`` +
    ``<target_id>.<judge>.body.txt`` (``preamble + body`` byte-identical to
    ``prompt.txt``) and record ``preamble_path`` / ``body_path`` on the manifest
    for headless fan-out. Grouped entries keep only ``prompt_path`` (don't stack
    grouping + caching).

    By default stale drafts from a prior run are cleared so ``commit`` never reads
    an orphan; pass ``keep_drafts=True`` to preserve already-written worker output
    during a recovery re-prepare (re-prepare is otherwise destructive).

    ``worker_model`` (default ``sonnet``) is the tier the orchestrator pins each
    spawned ``judge-worker`` to; ``batch_size`` (default 5) is the recommended
    fan-out per spawn wave. Nothing here calls an API; ``estimated_api_cost`` in
    ``usage_summary`` is the API-equivalent price shown for context only.
    """
    project_dir = Path(project_dir)
    context = dict(context or {})
    worker_model = worker_model or _DEFAULT_WORKER_MODEL
    batch_size = int(batch_size or _DEFAULT_BATCH_SIZE)
    targets_per_worker = max(1, int(targets_per_worker or 1))
    scope_list = [scopes] if isinstance(scopes, str) else list(scopes)

    # Resolve every scope into one ordered target list, deduped by target_id so an
    # overlapping chapter:/chunk: pair doesn't render (or get cost-estimated) twice.
    targets = []
    seen_targets: set[str] = set()
    for scope in scope_list:
        try:
            scope_targets = build_targets(project_dir, scope)
        except (NotImplementedError, FileNotFoundError, ValueError) as exc:
            # ScopeError subclasses ValueError; surface which scope failed.
            raise type(exc)(f"scope {scope!r}: {exc}") from exc
        for target in scope_targets:
            if target.id not in seen_targets:
                seen_targets.add(target.id)
                targets.append(target)

    for target in targets:
        if not _ID_RE.match(target.id):
            raise ValueError(f"target id contains unsafe characters: {target.id!r}")

    jdir = _judges_dir(project_dir)
    jdir.mkdir(parents=True, exist_ok=True)

    judge_instances = {name: get_judge(name) for name in judge_names}

    def _write_prompt(prompt: str, prompt_path: Path, draft_path: Path) -> None:
        prompt_path.write_text(prompt, encoding="utf-8")
        if not keep_drafts:
            draft_path.unlink(missing_ok=True)  # clear any stale draft from a prior prepare

    entries: list[dict[str, Any]] = []
    total_words = 0
    total_pairs = 0  # (target, judge) pairs — the batch-invariant unit of work
    batch_seq = 0
    # Per-judge shared preamble established by the first solo entry that splits.
    shared_preambles: dict[str, str] = {}
    # Grouping is per judge: a batched prompt combines targets for ONE judge,
    # since the shared rule block + item schema are judge-specific.
    for judge_name in judge_names:
        judge = judge_instances[judge_name]
        preamble_path = jdir / f"preamble.{judge_name}.txt"
        for group in _group_targets(targets, judge, targets_per_worker):
            total_pairs += len(group)
            if len(group) == 1:
                target = group[0]
                words = len((target.source_text or "").split())
                total_words += words
                prompt_path = jdir / f"{target.id}.{judge_name}.prompt.txt"
                draft_path = jdir / f"{target.id}.{judge_name}.draft.json"
                body_path = jdir / f"{target.id}.{judge_name}.body.txt"
                prefix, suffix = judge.build_prompt_parts(target, context)
                prompt = prefix + suffix
                _write_prompt(prompt, prompt_path, draft_path)
                entry: dict[str, Any] = {
                    "target_id": target.id,
                    "target_type": target.target_type,
                    "judge": judge_name,
                    "prompt_path": str(prompt_path),
                    "draft_path": str(draft_path),
                    "source_word_count": words,
                }
                # Cache split: only when the prefix is non-empty and matches the
                # per-judge shared preamble (or establishes it). Mismatch → omit
                # paths so fan-out uses the full prompt.txt.
                if prefix:
                    established = shared_preambles.get(judge_name)
                    if established is None:
                        shared_preambles[judge_name] = prefix
                        preamble_path.write_text(prefix, encoding="utf-8")
                        established = prefix
                    if prefix == established:
                        body_path.write_text(suffix, encoding="utf-8")
                        entry["preamble_path"] = str(preamble_path)
                        entry["body_path"] = str(body_path)
                    else:
                        body_path.unlink(missing_ok=True)
                else:
                    body_path.unlink(missing_ok=True)
                entries.append(entry)
            else:
                batch_id = f"batch_{batch_seq:03d}"
                batch_seq += 1
                prompt_path = jdir / f"{batch_id}.{judge_name}.prompt.txt"
                draft_path = jdir / f"{batch_id}.{judge_name}.draft.json"
                _write_prompt(judge.build_batch_prompt(group, context), prompt_path, draft_path)
                members = []
                for target in group:
                    words = len((target.source_text or "").split())
                    total_words += words
                    members.append(
                        {
                            "target_id": target.id,
                            "target_type": target.target_type,
                            "source_word_count": words,
                        }
                    )
                entries.append(
                    {
                        "batch_id": batch_id,
                        "judge": judge_name,
                        "prompt_path": str(prompt_path),
                        "draft_path": str(draft_path),
                        "members": members,
                    }
                )

    estimated_api_cost = estimate_suite_cost(judge_names, targets, context)

    manifest_doc = {
        "scopes": scope_list,
        "judges": judge_names,
        "worker_model": worker_model,
        "batch_size": batch_size,
        "targets_per_worker": targets_per_worker,
        "model": context.get("judge_model"),
        "provider": context.get("judge_provider"),
        "entries": entries,
    }
    manifest_path = jdir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "status": "ok",
        "manifest": entries,
        "manifest_path": str(manifest_path),
        "scopes": scope_list,
        "judges": judge_names,
        "worker_model": worker_model,
        "batch_size": batch_size,
        "usage_summary": {
            "pairs": total_pairs,
            "targets": len(targets),
            "workers": len(entries),
            "targets_per_worker": targets_per_worker,
            "source_words": total_words,
            "worker_model": worker_model,
            "batch_size": batch_size,
            "estimated_api_cost": estimated_api_cost,
        },
        "instructions": (
            "For each manifest entry (one target, or a small group of low-density "
            "targets) spawn one `judge-worker` subagent pinned to worker_model (Task "
            "tool, subagent_type=judge-worker, model=worker_model) that reads prompt_path "
            "and writes ONLY the JSON verdict to draft_path, in bounded batches of "
            "batch_size — or run `fanout` for a headless claude -p wave. Then run "
            "`commit`. Nothing here spends or calls an API."
            if entries
            else "Nothing to judge — scope resolved to no targets."
        ),
        "_schema": _PREPARE_SCHEMA,
    }


def _entry_fanout_id(entry: dict[str, Any]) -> str | None:
    """Stable id for a manifest entry: solo ``target_id`` or grouped ``batch_id``."""
    if entry.get("batch_id"):
        return str(entry["batch_id"])
    if entry.get("target_id"):
        return str(entry["target_id"])
    return None


def _read_draft_text(path: Path) -> str | None:
    """Read a draft file; return ``None`` when missing or unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def fanout(
    project_dir: Path,
    *,
    target_ids: list[str] | None = None,
    concurrency: int | None = None,
    claude_bin: str = "claude",
    runner=None,
) -> dict[str, Any]:
    """Run one headless ``claude -p`` wave for judge-prepare manifest entries.

    Opt-in alternative to Task-tool workers. For each selected entry that lacks a
    non-empty draft, invoke ``claude -p`` from a neutral cwd with ``--tools ""``
    and ``--output-format text``, writing stdout to ``draft_path``. When
    ``preamble_path`` + ``body_path`` are present, the body is the user prompt
    and the preamble is passed via ``--system-prompt-file``; otherwise the full
    ``prompt_path`` is used (grouped entries always take this path).

    ``target_ids``, when given, matches a solo entry's ``target_id`` or a batch
    entry's ``batch_id``. Does **not** call ``commit`` — the skill commits after
    the wave. ``runner`` is a test seam:
    ``(cmd, *, input_text, cwd) -> (rc, stdout, stderr)``.
    """
    from src.harness.headless import run_headless_wave

    project_dir = Path(project_dir)
    jdir = _judges_dir(project_dir)
    manifest_path = jdir / "manifest.json"
    if not manifest_path.exists():
        return {
            "error": "no judge manifest — run `prepare` first",
            "wrote": [],
            "failed": [],
            "skipped": [],
            "counts": {"wrote": 0, "failed": 0, "skipped": 0, "todo": 0},
            "_schema": _FANOUT_SCHEMA,
        }
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "error": f"unreadable manifest {manifest_path}: {exc}",
            "wrote": [],
            "failed": [],
            "skipped": [],
            "counts": {"wrote": 0, "failed": 0, "skipped": 0, "todo": 0},
            "_schema": _FANOUT_SCHEMA,
        }

    entries = [e for e in (doc.get("entries") or []) if isinstance(e, dict)]
    if target_ids is not None:
        wanted = set(target_ids)
        entries = [e for e in entries if _entry_fanout_id(e) in wanted]
        found = {_entry_fanout_id(e) for e in entries}
        missing_ids = wanted - found
        if missing_ids:
            return {
                "error": f"target_ids not in manifest: {sorted(missing_ids)}",
                "wrote": [],
                "failed": [],
                "skipped": [],
                "counts": {"wrote": 0, "failed": 0, "skipped": 0, "todo": 0},
                "_schema": _FANOUT_SCHEMA,
            }

    worker_model = doc.get("worker_model") or _DEFAULT_WORKER_MODEL
    if concurrency is None:
        try:
            concurrency = int(doc.get("batch_size") or _DEFAULT_BATCH_SIZE)
        except (TypeError, ValueError):
            concurrency = _DEFAULT_BATCH_SIZE
    if concurrency < 1:
        return {
            "error": f"invalid concurrency {concurrency!r}; must be >= 1",
            "wrote": [],
            "failed": [],
            "skipped": [],
            "counts": {"wrote": 0, "failed": 0, "skipped": 0, "todo": 0},
            "_schema": _FANOUT_SCHEMA,
        }

    skipped: list[str] = []
    ready: list[dict[str, Any]] = []
    pre_failed: list[dict[str, str]] = []
    for entry in entries:
        eid = _entry_fanout_id(entry)
        if not eid:
            pre_failed.append({"id": "?", "error": "malformed manifest entry: no target_id/batch_id"})
            continue
        draft_path = Path(entry["draft_path"])
        existing = _read_draft_text(draft_path) if draft_path.exists() else None
        if existing is not None and existing.strip():
            skipped.append(eid)
            continue
        prompt_path = Path(entry["prompt_path"])
        preamble = entry.get("preamble_path")
        body = entry.get("body_path")
        try:
            if preamble and body and Path(preamble).exists() and Path(body).exists():
                ready.append(
                    {
                        "id": eid,
                        "input_text": Path(body).read_text(encoding="utf-8"),
                        "output_path": str(draft_path),
                        "system_prompt_file": preamble,
                    }
                )
            elif not prompt_path.exists():
                pre_failed.append(
                    {"id": eid, "error": f"missing prompt_path: {prompt_path}"}
                )
            else:
                ready.append(
                    {
                        "id": eid,
                        "input_text": prompt_path.read_text(encoding="utf-8"),
                        "output_path": str(draft_path),
                        "system_prompt_file": None,
                    }
                )
        except OSError as exc:
            pre_failed.append(
                {"id": eid, "error": f"{type(exc).__name__}: {exc}"[:500]}
            )

    if not ready and not pre_failed:
        return {
            "wrote": [],
            "failed": [],
            "skipped": skipped,
            "worker_model": worker_model,
            "concurrency": concurrency,
            "cwd": None,
            "counts": {
                "wrote": 0,
                "failed": 0,
                "skipped": len(skipped),
                "todo": 0,
            },
            "instructions": (
                "Nothing to fan out — no matching manifest entries."
                if not skipped else
                "Run `commit` to land drafts. Re-run `fanout` (optionally with "
                "--target-ids) for any failed/missing, then commit again."
            ),
            "_schema": _FANOUT_SCHEMA,
        }

    wave_out = run_headless_wave(
        ready,
        model=worker_model,
        concurrency=concurrency,
        claude_bin=claude_bin,
        runner=runner,
    )

    if "error" in wave_out and not wave_out.get("wrote") and not wave_out.get("failed"):
        return {
            "error": wave_out["error"],
            "wrote": [],
            "failed": [],
            "skipped": skipped,
            "worker_model": worker_model,
            "concurrency": concurrency,
            "cwd": wave_out.get("cwd"),
            "counts": {
                "wrote": 0,
                "failed": 0,
                "skipped": len(skipped),
                "todo": 0,
            },
            "_schema": _FANOUT_SCHEMA,
            "instructions": "Fix the launcher error, then re-run `fanout`.",
        }

    failed = list(pre_failed)
    failed.extend(wave_out.get("failed") or [])
    wrote = list(wave_out.get("wrote") or [])

    return {
        "wrote": wrote,
        "failed": failed,
        "skipped": skipped,
        "worker_model": worker_model,
        "concurrency": concurrency,
        "cwd": wave_out.get("cwd"),
        "counts": {
            "wrote": len(wrote),
            "failed": len(failed),
            "skipped": len(skipped),
            "todo": len(ready) + len(pre_failed),
        },
        "instructions": (
            "Run `commit` to land drafts. Re-run `fanout` (optionally with "
            "--target-ids) for any failed/missing, then commit again."
            if (wrote or failed or skipped)
            else "Nothing to fan out — no matching manifest entries."
        ),
        "_schema": _FANOUT_SCHEMA,
    }


def commit(project_dir: Path, *, persist: bool = False) -> dict[str, Any]:
    """Collect ``judge-worker`` drafts, parse them, and (optionally) persist.

    Reads the ``prepare`` manifest; for each entry reads the worker's draft and
    calls ``judge.parse_response`` — the *same* parser the API path uses. A draft
    that parses is ``committed`` (and, with ``persist`` and a chunk target, written
    to ``evaluations/<chunk>.json`` via ``merge_judge_result`` — identical to the
    API ``--persist`` path). A draft that does not parse is ``failed``; an absent
    draft is ``missing``. Re-spawn ``failed`` + ``missing`` and re-run commit
    (idempotent: re-parsing a draft just re-persists the same result).

    A **grouped** entry (from ``prepare(targets_per_worker > 1)``) holds one draft
    with a ``verdicts`` object keyed by target id; it is split per member and each
    verdict fed through the same ``parse_response`` seam, attributed to its own
    target. A member id absent from ``verdicts`` is ``missing`` (never silently
    dropped); a malformed per-item verdict is ``failed``; and an unparseable batch
    draft fails every member — so re-spawn/recovery is per target, and everything
    is accounted for exactly as in the solo path.
    """
    project_dir = Path(project_dir)
    jdir = _judges_dir(project_dir)
    manifest_path = jdir / "manifest.json"
    if not manifest_path.exists():
        return {
            "status": "error",
            "error": "no judge manifest — run `prepare` first",
            "committed": [],
            "failed": [],
            "missing": [],
            "_schema": _COMMIT_SCHEMA,
        }

    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "status": "error",
            "error": f"unreadable manifest {manifest_path}: {exc}",
            "committed": [],
            "failed": [],
            "missing": [],
            "_schema": _COMMIT_SCHEMA,
        }

    entries = doc.get("entries", [])
    if not isinstance(entries, list):
        return {
            "status": "error",
            "error": f"manifest 'entries' is not a list: {type(entries).__name__}",
            "committed": [],
            "failed": [],
            "missing": [],
            "_schema": _COMMIT_SCHEMA,
        }
    worker_model = doc.get("worker_model") or _DEFAULT_WORKER_MODEL
    context = {
        "judge_model": doc.get("model"),
        "judge_provider": doc.get("provider"),
    }

    committed: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    results = []
    persisted: Optional[list[str]] = [] if persist else None
    persist_errors: Optional[list[str]] = [] if persist else None
    judge_cache: dict[str, Any] = {}

    def _commit_member(
        target_id: str, target_type: str, judge_name: str, verdict_raw: str
    ) -> None:
        """Parse one member's verdict, stamp it, record it, and optionally persist.

        Shared by the solo path (whole draft = one verdict) and the batch path
        (one verdict split from the ``verdicts`` object) so both go through the
        same ``parse_response`` seam the API path uses.
        """
        target = JudgeTarget(
            id=target_id,
            target_type=target_type,
            source_text="",
            translated_text="",
            context={},
        )
        try:
            judge = judge_cache.setdefault(judge_name, get_judge(judge_name))
            result = judge.parse_response(target, verdict_raw, context)
        except JudgeParseError as exc:
            failed.append({"target_id": target_id, "judge": judge_name, "problem": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - one bad verdict must not sink the batch
            logger.error("judge-commit: %s/%s parse crashed: %s", target_id, judge_name, exc)
            failed.append(
                {"target_id": target_id, "judge": judge_name, "problem": f"{type(exc).__name__}: {exc}"}
            )
            return

        # Stamp how this result was produced so a persisted judge entry is
        # self-describing (the API path records model/provider; here we record
        # the worker tier + backend).
        result.metadata["backend"] = "subagent"
        result.metadata["worker_model"] = worker_model

        results.append(result)
        committed.append({"target_id": target_id, "judge": judge_name})

        if persist and target_type == "chunk":
            from web_ui.evaluations import merge_judge_result

            try:
                path = merge_judge_result(
                    project_dir, target_id, judge_name, result.model_dump(mode="json")
                )
                persisted.append(str(path))
            except Exception as exc:  # noqa: BLE001 - persist errors must not corrupt output
                logger.error(
                    "judge-commit: failed to persist %s/%s: %s", target_id, judge_name, exc
                )
                persist_errors.append(f"{target_id}/{judge_name}: {exc}")

    for entry in entries:
        if not isinstance(entry, dict):
            failed.append({"target_id": "?", "judge": "?", "problem": f"manifest entry not an object: {type(entry).__name__}"})
            continue

        judge_name = entry.get("judge")
        draft_path_raw = entry.get("draft_path")
        members = _entry_members(entry)
        if not judge_name or not draft_path_raw or not members:
            failed.append(
                {"target_id": "?", "judge": judge_name or "?", "problem": "malformed manifest entry: missing judge, draft_path, or target(s)"}
            )
            continue

        # A batch entry may carry a corrupt member alongside valid ones; record
        # each so it's accounted for rather than silently dropped by _entry_members.
        for bad in _entry_malformed_members(entry):
            failed.append(
                {"target_id": "?", "judge": judge_name, "problem": f"malformed batch member (no target_id): {bad!r}"}
            )
        draft_path = Path(draft_path_raw)

        if not draft_path.resolve().is_relative_to(_judges_dir(project_dir).resolve()):
            for member in members:
                failed.append({"target_id": member["target_id"], "judge": judge_name, "problem": f"draft_path escapes judges dir: {draft_path}"})
            continue

        if not draft_path.exists():
            for member in members:
                missing.append({"target_id": member["target_id"], "judge": judge_name})
            continue

        try:
            raw = draft_path.read_text(encoding="utf-8")
        except OSError as exc:
            for member in members:
                failed.append({"target_id": member["target_id"], "judge": judge_name, "problem": f"unreadable draft {draft_path}: {exc}"})
            continue

        if _is_batch_entry(entry):
            try:
                verdicts = _parse_batch_verdicts(raw)
            except JudgeParseError as exc:
                for member in members:
                    failed.append({"target_id": member["target_id"], "judge": judge_name, "problem": f"batch draft: {exc}"})
                continue
            for member in members:
                target_id = member["target_id"]
                if target_id not in verdicts:
                    missing.append({"target_id": target_id, "judge": judge_name})
                    continue
                verdict = verdicts[target_id]
                if verdict is None:
                    # An explicit null verdict is a bad answer, not an omission —
                    # fail it (don't route to re-spawn as if it were absent).
                    failed.append(
                        {"target_id": target_id, "judge": judge_name, "problem": "batch verdict is null"}
                    )
                    continue
                _commit_member(
                    target_id,
                    member.get("target_type", "chunk"),
                    judge_name,
                    json.dumps(verdict, ensure_ascii=False),
                )
        else:
            member = members[0]
            _commit_member(member["target_id"], member.get("target_type", "chunk"), judge_name, raw)

    judge_names = doc.get("judges") or list(
        dict.fromkeys(e.get("judge") for e in entries if isinstance(e, dict) and e.get("judge"))
    )
    all_target_ids: set[str] = set()
    for e in entries:
        if isinstance(e, dict):
            for member in _entry_members(e):
                all_target_ids.add(member["target_id"])
    run_header = build_run_header(
        judge_names,
        target_count=len(all_target_ids),
        model=context["judge_model"],
        provider=context["judge_provider"],
        backend="subagent",
        worker_model=worker_model,
    )

    return {
        "status": "ok",
        "committed": committed,
        "failed": failed,
        "missing": missing,
        "counts": {
            "committed": len(committed),
            "failed": len(failed),
            "missing": len(missing),
        },
        "summary": aggregate_results(results),
        "run_header": run_header,
        "results": [r.model_dump(mode="json") for r in results],
        "persisted": persisted,
        "persist_errors": persist_errors,
        "_schema": _COMMIT_SCHEMA,
    }
