"""
The scan wave: render one prompt per chapter, fan it out, collect candidates.

Structurally a twin of ``src/annotations/review.py``'s prepare/fanout/commit, with
two deliberate differences.

**The unit of work is a chapter, not a note.** A scan has nothing to key off yet —
it is looking for spots, so the whole chapter is the body and one worker reads it
end to end.

**The wave only detects.** A candidate is ``{chapter_id, es_idx, quoted_span,
category, claim, why}`` — a pointer, never a gloss. No style guide and no glossary
go into the scan prompt: those govern how a gloss is *worded*, they load at the
drafting step instead, and a scanner given them starts writing notes, which is the
wrong output and wastes the wave. The English source is withheld for a second,
separate reason — see :func:`render_body`. One consequence worth stating: the
annotation-review cache split (``docs/ANNOTATION_REVIEW.md``) notes that the
glossary is what carried *that* preamble over Sonnet's 1024-token cache minimum,
and this one has no glossary to lean on — measured on ``fabre2`` it lands at ~1.1k
estimated tokens against a ~2.7k chapter body, barely over the line, and a terser
profile would fall under it. That is a cost detail, not a correctness one, and the
body dominates the job either way. Do not pad the prompt to hit the threshold.

``scan_commit`` validates every candidate before the user ever sees it: the
``es_idx`` must exist in the alignment and the quoted span must occur in that
aligned sentence. A candidate that fails is reported as ``unusable`` rather than
offered as a choice — a hallucinated span cannot become an anchor.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.footnote_pass.corpus import (
    already_noted_keys,
    footnotes_dir,
    load_alignment_rows,
)
from src.judges.base import _CACHE_PREFIX_SPLIT_MARKER
from src.judges.llm_io import (
    JudgeParseError,
    load_template,
    parse_judge_json,
    prompt_version,
    render,
)

logger = logging.getLogger(__name__)

TEMPLATE = "footnote_scan.txt"

# Wave type, for ``state.COMMAND_EFFORT_DEFAULTS`` / ``headless_effort_footnote_scan``.
COMMAND = "footnote_scan"

_DEFAULT_BATCH_SIZE = 5

# Keys every scan draft must carry.
REQUIRED_FIELDS = ("chapter_id", "candidates")

# Candidate fields the commit validator requires before it will show one.
CANDIDATE_FIELDS = ("es_idx", "quoted_span", "category", "claim", "why")

# What `--source-text` accepts. "es" is the default; see `render_body`.
SOURCE_TEXT_CHOICES = ("es", "both")
DEFAULT_SOURCE_TEXT = "es"

# The ``{{source_rule}}`` paragraph, one per mode. Two strings rather than two
# template files: a second template is a second thing to keep in step, and
# everything else about the two prompts is identical.
#
# Wrapped to the template's own column, because they are pasted into it.
_SOURCE_RULE = {
    "es": (
        "You are reading the Spanish ALONE. The English original is not in front of\n"
        "you, and you must not guess at it. State the `claim` as *this sentence* makes\n"
        "it — your candidate reaches the researcher with the English source attached\n"
        "beside it, and what the author actually asserted is settled there, before a\n"
        "word of the gloss is written.\n"
        "\n"
        "So do not propose a sentence because you suspect the translation of it. A\n"
        "rendering that reads oddly is the judges' business, not the back matter's, and\n"
        "from here you cannot tell a translator's slip from an author's claim. Propose\n"
        "the claim; let the researcher check the source."
    ),
    "both": (
        "Judge the claim against the **EN** line as well as the ES. Most candidates are\n"
        "of the form \"the author asserts X\", and what the author asserted is in the\n"
        "source."
    ),
}


def _manifest_path(project_dir: Path) -> Path:
    """This wave's manifest.

    Named ``scan.manifest.json``, not ``manifest.json``, because
    ``harness.py footnotes`` — the *translation* wave for imported Gutenberg notes
    — writes ``.harness/footnotes/manifest.json`` from
    ``src/harness/flow.py:_footnote_manifest_path``. Two waves, one directory, one
    filename: whichever prepared last silently owned it. The ``.scan.`` infix is
    the convention every other file this wave writes already uses
    (``<chapter_id>.scan.prompt.txt``).
    """
    return footnotes_dir(project_dir) / "scan.manifest.json"


def _usage_log_path(project_dir: Path) -> Path:
    """Per-job rows for this wave, split from the translation wave's log.

    Same collision as :func:`_manifest_path`, and here it is not just ownership:
    ``profile.baseline_tokens`` reads this file to price the consent gate, and a
    median taken across two wave types with very different prompt sizes describes
    neither. Registered in ``profile.USAGE_LOG_RELPATH`` under ``footnote_scan``.
    """
    return footnotes_dir(project_dir) / "scan.usage.jsonl"


def _candidates_path(project_dir: Path) -> Path:
    return footnotes_dir(project_dir) / "candidates.json"


def chapter_ids(project_dir: Path) -> list[str]:
    """Every chapter that has an alignment file, in book order.

    The alignment is the gate, not ``chapters/``: a chapter with no alignment has
    no ``es_idx`` space, so nothing in it can be anchored and it cannot be scanned.
    """
    align_dir = Path(project_dir) / "alignments"
    if not align_dir.is_dir():
        return []
    return sorted(p.stem for p in align_dir.glob("*.json"))


def render_body(
    chapter_id: str,
    rows: list[dict],
    already: set[tuple[str, Optional[int]]],
    *,
    include_source: bool = False,
) -> str:
    """The per-chapter body: numbered ``es_idx | ES`` rows, ``| EN`` on request.

    **The scan reads the Spanish alone.** This is the shape
    :mod:`src.judges.editorial_judge` already uses and states the reason for: a
    reader who can see the original stops evaluating the Spanish as Spanish and
    starts diffing it against the source. A footnote is for someone holding the
    translation and nothing else, so the pass that decides whether a sentence
    needs one should be reading what that reader reads.

    The English is not discarded, only moved. ``scan_commit`` attaches
    ``en_sentence`` to every usable candidate from the alignment and
    ``report.render_candidate_report`` prints it, so the source rejoins the
    candidate at the review gate — where a human and the researching agent are
    already looking, and can act on it. Same division of labour as
    :mod:`src.judges.editorial_verify`, and attached to *every* candidate rather
    than to the ones a scanner nominates for the reason that module gives: the
    2026-08-27 judge-review friction log caught a wave where nothing asked for
    the source, so pass two adjudicated blind and confirmed everything.

    ``include_source=True`` (``scan-prepare --source-text both``) restores the EN
    line. It is worth a deliberate choice rather than a default: measured across
    the 40 prepared ``fabre2`` bodies the EN lines are 48% of the body, so
    carrying them very nearly doubles the wave's input.

    Sentences that already carry a footnote are marked inline, which is cheaper and
    more reliable than a separate exclusion list the model has to cross-reference.
    """
    lines = [f"CHAPTER: {chapter_id}", f"SENTENCES: {len(rows)}", ""]
    noted = {idx for (ch, idx) in already if ch == chapter_id}
    if noted:
        lines += [
            f"Sentences already carrying a footnote (NEVER propose these): "
            f"{sorted(i for i in noted if i is not None)}",
            "",
        ]
    for row in rows:
        es_idx = row.get("es_idx")
        marker = "  [ALREADY NOTED]" if es_idx in noted else ""
        lines.append(f"[{es_idx}]{marker}")
        lines.append(f"  ES: {(row.get('es') or '').strip()}")
        if include_source:
            lines.append(f"  EN: {(row.get('en') or '').strip()}")
    lines += [
        "",
        "Return the JSON object for this chapter. An empty `candidates` list is a "
        "valid and common answer.",
    ]
    return "\n".join(lines)


def build_prompt_parts(
    profile: str,
    chapter_id: str,
    rows: list[dict],
    already: set[tuple[str, Optional[int]]],
    *,
    include_source: bool = False,
) -> tuple[str, str]:
    """``(preamble, body)`` for one chapter. The preamble is identical per wave.

    ``source_rule`` sits above the cache-split marker, so each mode gets its own
    per-wave cached preamble — and a body that carries the EN can never be served
    under the preamble that says the English is not in front of you.
    """
    rendered = render(
        load_template(TEMPLATE),
        {
            "profile": profile,
            "source_rule": _SOURCE_RULE["both" if include_source else "es"],
        },
    )
    prefix, marker, suffix = rendered.partition(_CACHE_PREFIX_SPLIT_MARKER)
    body = render_body(chapter_id, rows, already, include_source=include_source)
    if not marker:
        # No split marker: the whole template is the body, exactly how judges and
        # annotation prompts degrade.
        return "", rendered.rstrip("\n") + "\n\n" + body
    # The template's own tail below the marker is usually empty (the rows are the
    # whole body), so join rather than concatenate: a body that opens with blank
    # lines is the first thing the worker reads.
    tail = suffix.strip("\n")
    return prefix, (tail + "\n\n" + body if tail else body)


_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "manifest": "one entry per chapter: {chapter_id, sentences, already_noted, "
    "prompt_path, draft_path, preamble_path, body_path}",
    "manifest_path": "path to scan.manifest.json (scan-fanout and scan-commit read this)",
    "profile_path": "the approved Gate 1 profile this wave was rendered against",
    "chapters": "chapter ids in scope",
    "source_text": "'es' (default — the scanner reads the Spanish alone) or 'both'. "
    "prompt_version hashes the template, which is shared by the two modes, so this "
    "is the only field that says which prompt a candidate set came from",
    "worker_model": "model tier to pin each footnote-scan-worker to",
    "batch_size": "workers per wave / default headless concurrency",
    "effective": "what the wave will run as, with provenance per field: "
    "{cli, cli_source, worker_model, worker_model_source, effort, effort_source, "
    "effort_channel, baseline_tokens, baseline_source, host, warnings}. Quote this "
    "at the usage gate — the four fields cli/worker_model/effort/effort_channel are "
    "only interpretable together",
    "usage_summary": "{chapters, workers, sentences, already_noted, worker_model, "
    "batch_size, cli, headless_baseline_tokens, headless_baseline_source, "
    "headless_effort, headless_effort_source, headless_effort_channel}",
    "instructions": "what to do with the manifest (scan-fanout, or spawn workers)",
}


def scan_prepare(
    project_dir: Path,
    *,
    profile_file: Path,
    chapters: Optional[list[str]] = None,
    worker_model: Optional[str] = None,
    batch_size: Optional[int] = None,
    keep_drafts: bool = False,
    source_text: str = DEFAULT_SOURCE_TEXT,
) -> dict[str, Any]:
    """Render one scan prompt per chapter plus a manifest (no spend).

    ``profile_file`` is **required** — that is how Gate 1 is enforced in code
    rather than by prose. It points at the taxonomy the user approved, which the
    agent wrote with ``Write`` after the gate. Without it there is nothing to scan
    *for*, and a wave would run on the model's own idea of what deserves a note.

    ``source_text`` picks which languages the scanner reads; see
    :func:`render_body`. It is recorded on the manifest and in the payload because
    ``prompt_version`` cannot distinguish the two modes — they share a template.
    """
    import sys

    from src.harness.profile import resolve_profile

    project_dir = Path(project_dir)
    profile_file = Path(profile_file)
    source_text = str(source_text or DEFAULT_SOURCE_TEXT).strip().lower()
    if source_text not in SOURCE_TEXT_CHOICES:
        return {
            "status": "error",
            "error": (
                f"unknown source_text {source_text!r}; expected one of "
                f"{list(SOURCE_TEXT_CHOICES)}"
            ),
            "_schema": _PREPARE_SCHEMA,
        }
    include_source = source_text == "both"
    if not profile_file.exists():
        return {
            "status": "error",
            "error": (
                f"profile file not found: {profile_file}. `scan-prepare` requires "
                "--profile-file: the approved footnote profile from Gate 1. Run "
                "`style`, agree the taxonomy with the user, and Write it first."
            ),
            "_schema": _PREPARE_SCHEMA,
        }
    try:
        profile = profile_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "status": "error",
            "error": f"could not read profile file {profile_file}: {exc}",
            "_schema": _PREPARE_SCHEMA,
        }
    if not profile:
        return {
            "status": "error",
            "error": f"profile file is empty: {profile_file}",
            "_schema": _PREPARE_SCHEMA,
        }

    available = chapter_ids(project_dir)
    if not available:
        return {
            "status": "error",
            "error": (
                f"no alignment files in {project_dir / 'alignments'} — a chapter "
                "needs an alignment before anything in it can be anchored"
            ),
            "_schema": _PREPARE_SCHEMA,
        }
    if chapters:
        wanted = [c for c in available if c in set(chapters)]
        unknown = sorted(set(chapters) - set(available))
        if unknown:
            return {
                "status": "error",
                "error": f"chapters not found in alignments/: {unknown}",
                "_schema": _PREPARE_SCHEMA,
            }
    else:
        wanted = available

    # One resolver, not three. `resolve_cli` + `default_worker_model` +
    # `resolve_headless_argv` each answered a slightly different question, which
    # is how a payload came to print `headless_effort: medium` beside a
    # `grok-4.6[effort=high,fast=false]` worker — the consent gate then had two
    # true-looking numbers to choose between and picked the inert one.
    prof = resolve_profile(
        project_dir,
        command=COMMAND,
        worker_model=worker_model,
        usage_log=_usage_log_path(project_dir),
    )
    worker_model = prof.worker_model
    batch_size = _DEFAULT_BATCH_SIZE if batch_size is None else max(1, int(batch_size))

    fdir = footnotes_dir(project_dir)
    fdir.mkdir(parents=True, exist_ok=True)
    already = already_noted_keys(project_dir)

    entries: list[dict[str, Any]] = []
    preamble_path = fdir / "preamble.scan.txt"
    established: Optional[str] = None
    total_sentences = 0

    for chapter_id in wanted:
        rows = load_alignment_rows(project_dir, chapter_id)
        prefix, body = build_prompt_parts(
            profile, chapter_id, rows, already, include_source=include_source
        )
        total_sentences += len(rows)

        prompt_path = fdir / f"{chapter_id}.scan.prompt.txt"
        body_path = fdir / f"{chapter_id}.scan.body.txt"
        draft_path = fdir / f"{chapter_id}.scan.draft.json"

        prompt_path.write_text(prefix + body, encoding="utf-8")
        if not keep_drafts:
            draft_path.unlink(missing_ok=True)

        entry: dict[str, Any] = {
            "chapter_id": chapter_id,
            "sentences": len(rows),
            "already_noted": sorted(
                i for (ch, i) in already if ch == chapter_id and i is not None
            ),
            "prompt_path": str(prompt_path),
            "draft_path": str(draft_path),
            "prompt_version": prompt_version(TEMPLATE),
        }

        # Cache split: the preamble is per wave, so the first chapter establishes
        # it and the rest must match byte-for-byte. A mismatch means something
        # chapter-specific leaked above the marker — drop the split for that entry
        # rather than serve a wrong preamble.
        if prefix:
            if established is None:
                established = prefix
                preamble_path.write_text(prefix, encoding="utf-8")
            if prefix == established:
                body_path.write_text(body, encoding="utf-8")
                entry["preamble_path"] = str(preamble_path)
                entry["body_path"] = str(body_path)
            else:
                body_path.unlink(missing_ok=True)
        else:
            body_path.unlink(missing_ok=True)

        entries.append(entry)

    manifest_doc = {
        "chapters": wanted,
        "profile_path": str(profile_file),
        "worker_model": worker_model,
        "batch_size": batch_size,
        "prepared_at": datetime.now().isoformat(),
        "prompt_version": prompt_version(TEMPLATE),
        # Beside `prompt_version`, not folded into it: the hash covers the
        # template, and both modes render from the same one.
        "source_text": source_text,
        # The resolved profile, so `fanout` reproduces the consented wave without
        # the operator re-passing --cli, and a wrong pin is visible on disk rather
        # than only in a payload that scrolled away.
        "cli": prof.cli,
        "effort": prof.effort,
        "effort_channel": prof.effort_channel,
        "host": prof.host,
        "entries": entries,
    }
    manifest_path = _manifest_path(project_dir)
    manifest_path.write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Also on stderr: this is the last moment before the manifest is spawned
    # against, and a mis-resolved CLI is worth seeing even by a caller who only
    # skims the JSON.
    for warning in prof.warnings:
        print(f"[scan-prepare] warning: {warning}", file=sys.stderr)

    return {
        "status": "ok",
        "manifest": entries,
        "manifest_path": str(manifest_path),
        "profile_path": str(profile_file),
        "chapters": wanted,
        "worker_model": worker_model,
        "batch_size": batch_size,
        "source_text": source_text,
        "effective": prof.to_payload(),
        "usage_summary": {
            "chapters": len(entries),
            "workers": len(entries),
            "sentences": total_sentences,
            "already_noted": sum(len(e["already_noted"]) for e in entries),
            "worker_model": worker_model,
            "batch_size": batch_size,
            "source_text": source_text,
            "cli": prof.cli,
            "headless_baseline_tokens": prof.baseline_tokens,
            "headless_baseline_source": prof.baseline_source,
            # Off the same profile as `effective`, so these two can no longer
            # tell the operator different stories about the same wave.
            "headless_effort": prof.effort,
            "headless_effort_source": prof.effort_source,
            "headless_effort_channel": prof.effort_channel,
        },
        "instructions": (
            "Run `scan-fanout` for a headless wave, or spawn one "
            "`footnote-scan-worker` per manifest entry (Task tool, pinned to "
            "worker_model) in waves of batch_size. Then `scan-commit`. This wave "
            "only DETECTS — it must not write glosses."
        ),
        "_schema": _PREPARE_SCHEMA,
    }


_FANOUT_SCHEMA = {
    "wrote": "chapter ids whose drafts were written this wave",
    "failed": "list of {id, error} — re-run scan-fanout for these",
    "skipped": "chapter ids that already had a non-empty draft",
    "worker_model": "model tier used for the headless CLI",
    "concurrency": "max parallel headless CLI processes",
    "cli": "headless CLI used (claude|cursor)",
    "source_text": "which languages the prepared bodies carry ('es'|'both'), echoed "
    "from the manifest — fanout re-renders nothing, it ships what prepare wrote",
    "effective": "what the wave ran as, with provenance per field — the same "
    "block scan-prepare printed, re-resolved from the manifest",
    "warning": "optional non-fatal notice (e.g. Cursor paired with a Claude model alias)",
    "counts": "{wrote, failed, skipped, todo}",
    "usage": "what the wave consumed; per-job detail goes to "
    ".harness/footnotes/scan.usage.jsonl, never into this payload",
    "instructions": "next step (scan-commit, or re-fanout failed/missing)",
}


def _fanout_error(message: str, **extra: Any) -> dict[str, Any]:
    out = {
        "error": message,
        "wrote": [],
        "failed": [],
        "skipped": [],
        "counts": {"wrote": 0, "failed": 0, "skipped": 0, "todo": 0},
        "_schema": _FANOUT_SCHEMA,
    }
    out.update(extra)
    return out


def scan_fanout(
    project_dir: Path,
    *,
    target_ids: Optional[list[str]] = None,
    concurrency: Optional[int] = None,
    cli: Optional[str] = None,
    cli_bin: Optional[str] = None,
    effort: Optional[str] = None,
    cache: Optional[str] = None,
    runner=None,
) -> dict[str, Any]:
    """Run one headless CLI wave over the prepared chapters.

    Each job passes the shared preamble as ``system_prompt_file`` and the
    chapter's ``es_idx | ES`` rows as ``input_text`` (``| EN`` when
    ``source_text`` is ``both``). ``run_headless_wave`` owns the CLI
    difference, the subscription preflight and the usage log — no launcher code
    lives here.

    ``runner`` is a test seam: ``(cmd, *, input_text, cwd) -> (rc, stdout, stderr)``.
    """
    import sys

    from src.harness import state as hstate
    from src.harness.headless import run_headless_wave
    from src.harness.profile import resolve_profile

    project_dir = Path(project_dir)
    cfg = hstate.load_config(project_dir)
    requested_cache = hstate.resolve_prompt_cache(cfg, cache_override=cache)

    manifest_path = _manifest_path(project_dir)
    if not manifest_path.exists():
        return _fanout_error("no footnote scan manifest — run `scan-prepare` first")
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return _fanout_error(f"unreadable manifest {manifest_path}: {exc}")

    entries = [e for e in (doc.get("entries") or []) if isinstance(e, dict)]
    if target_ids is not None:
        wanted = set(target_ids)
        entries = [e for e in entries if e.get("chapter_id") in wanted]
        missing = wanted - {e.get("chapter_id") for e in entries}
        if missing:
            return _fanout_error(f"chapters not in manifest: {sorted(missing)}")

    # Reproduce the consented wave. The manifest is an explicit choice, not a
    # guess, so its values are labelled `manifest` rather than `cli` — "a flag
    # said so" and "the manifest we were consented to said so" must not print
    # identically.
    #
    # A manifest that recorded `effort: null` means "emit no flag", which the
    # resolver spells "default"; inheriting it as None would fall back through
    # the config ladder and run at a level nobody approved. Only inherit while
    # the CLI has not flipped, though — a level resolved for Claude is a
    # Claude-table number, and carrying it onto Cursor would write an
    # `[effort=…]` bracket onto a model that never had one.
    inherited_effort, inherited_effort_source = effort, "cli"
    if not effort and "effort" in doc and (cli or doc.get("cli")) == doc.get("cli"):
        inherited_effort = doc["effort"] or "default"
        inherited_effort_source = "manifest"

    prof = resolve_profile(
        project_dir,
        command=COMMAND,
        cli=cli or doc.get("cli"),
        cli_source="cli" if cli else "manifest",
        worker_model=doc.get("worker_model"),
        worker_model_source="manifest",
        effort=inherited_effort,
        effort_source=inherited_effort_source,
        cfg=cfg,
        usage_log=_usage_log_path(project_dir),
    )
    cli_name = prof.cli
    worker_model = prof.worker_model
    resolved_effort = prof.effort
    # Only the argv channel takes a `--effort` flag. On Cursor the level rides in
    # the model's own `[effort=…]` bracket, and feeding it to the argv composer
    # would put the flag on a CLI that has none.
    extra_flags = hstate.compose_headless_argv(
        cfg, resolved_effort if prof.effort_channel == "argv" else None
    )
    for warning in prof.warnings:
        print(f"[scan-fanout] warning: {warning}", file=sys.stderr)
    # One joined string, plus the list: callers and tests substring-match
    # `warning` for a specific notice, which a bare list would break.
    model_warning = "; ".join(prof.warnings) or None

    if concurrency is None:
        try:
            concurrency = int(doc.get("batch_size") or _DEFAULT_BATCH_SIZE)
        except (TypeError, ValueError):
            concurrency = _DEFAULT_BATCH_SIZE
    if concurrency < 1:
        return _fanout_error(f"invalid concurrency {concurrency!r}; must be >= 1")

    fdir = footnotes_dir(project_dir).resolve()
    ready: list[dict[str, Any]] = []
    pre_failed: list[dict[str, str]] = []
    skipped: list[str] = []

    for entry in entries:
        chapter_id = entry.get("chapter_id")
        if not chapter_id:
            pre_failed.append({"id": "?", "error": "malformed manifest entry: no chapter_id"})
            continue
        draft_raw = entry.get("draft_path")
        prompt_raw = entry.get("prompt_path")
        if not draft_raw or not prompt_raw:
            pre_failed.append(
                {
                    "id": chapter_id,
                    "error": "malformed manifest entry: missing draft_path or prompt_path",
                }
            )
            continue

        draft_path = Path(draft_raw)
        prompt_path = Path(prompt_raw)
        preamble = entry.get("preamble_path")
        body = entry.get("body_path")

        # Same confinement scan_commit enforces: a hand-edited manifest must not
        # point the launcher at paths outside `.harness/footnotes/`.
        candidates = [draft_path, prompt_path]
        if preamble:
            candidates.append(Path(preamble))
        if body:
            candidates.append(Path(body))
        try:
            escaped = [str(p) for p in candidates if not p.resolve().is_relative_to(fdir)]
        except (OSError, RuntimeError, ValueError) as exc:
            pre_failed.append(
                {"id": chapter_id, "error": f"unresolvable path: {type(exc).__name__}: {exc}"[:500]}
            )
            continue
        if escaped:
            pre_failed.append(
                {"id": chapter_id, "error": f"path escapes footnotes dir: {escaped[0]}"}
            )
            continue

        if draft_path.exists():
            try:
                if draft_path.read_text(encoding="utf-8").strip():
                    skipped.append(chapter_id)
                    continue
            except (OSError, UnicodeDecodeError):
                pass

        try:
            if preamble and body and Path(preamble).exists() and Path(body).exists():
                ready.append(
                    {
                        "id": chapter_id,
                        "input_text": Path(body).read_text(encoding="utf-8"),
                        "output_path": str(draft_path),
                        "system_prompt_file": preamble,
                    }
                )
            elif not prompt_path.exists():
                pre_failed.append({"id": chapter_id, "error": f"missing prompt_path: {prompt_path}"})
            else:
                ready.append(
                    {
                        "id": chapter_id,
                        "input_text": prompt_path.read_text(encoding="utf-8"),
                        "output_path": str(draft_path),
                        "system_prompt_file": None,
                    }
                )
        except OSError as exc:
            pre_failed.append({"id": chapter_id, "error": f"{type(exc).__name__}: {exc}"[:500]})

    base = {
        "worker_model": worker_model,
        "cli": cli_name,
        "concurrency": concurrency,
        # Echoed, never re-derived: the bodies on disk are what they are, and a
        # payload that guessed the mode could contradict the file it is shipping.
        "source_text": doc.get("source_text") or DEFAULT_SOURCE_TEXT,
        # Every exit from here — launcher error, empty wave, completed wave — has
        # to say what it ran (or would have run) as. A payload that omits it is
        # how an operator ends up reading a Claude effort beside a Cursor wave.
        "effective": prof.to_payload(),
        "_schema": _FANOUT_SCHEMA,
    }
    if model_warning:
        base["warning"] = model_warning
        base["warnings"] = list(prof.warnings)

    if not ready:
        return {
            **base,
            "wrote": [],
            "failed": list(pre_failed),
            "skipped": skipped,
            "cwd": None,
            "counts": {
                "wrote": 0,
                "failed": len(pre_failed),
                "skipped": len(skipped),
                "todo": len(pre_failed),
            },
            "instructions": (
                "Fix the failed entries, then re-run `scan-fanout`."
                if pre_failed
                else (
                    "Run `scan-commit` to land drafts."
                    if skipped
                    else "Nothing to fan out — no matching manifest entries."
                )
            ),
        }

    wave = run_headless_wave(
        ready,
        model=worker_model,
        concurrency=concurrency,
        cli=cli_name,
        cli_bin=cli_bin,
        runner=runner,
        usage_log=_usage_log_path(project_dir),
        extra_flags=extra_flags,
        effort=resolved_effort,
        cache=requested_cache,
    )

    if "error" in wave and not wave.get("wrote") and not wave.get("failed"):
        return {
            **base,
            "error": wave["error"],
            "wrote": [],
            "failed": [],
            "skipped": skipped,
            "cwd": wave.get("cwd"),
            "counts": {"wrote": 0, "failed": 0, "skipped": len(skipped), "todo": 0},
            "instructions": "Fix the launcher error, then re-run `scan-fanout`.",
        }

    failed = list(pre_failed) + list(wave.get("failed") or [])
    wrote = list(wave.get("wrote") or [])
    out = {
        **base,
        "wrote": wrote,
        "failed": failed,
        "skipped": skipped,
        "cwd": wave.get("cwd"),
        "counts": {
            "wrote": len(wrote),
            "failed": len(failed),
            "skipped": len(skipped),
            "todo": len(ready) + len(pre_failed),
        },
        "instructions": (
            "Run `scan-commit` to land drafts. Re-run `scan-fanout` (optionally "
            "with --target-ids) for any failed/missing, then commit again."
        ),
    }
    if wave.get("usage"):
        out["usage"] = wave["usage"]
    return out


def parse_scan_draft(raw: str, *, chapter_id: str) -> list[dict[str, Any]]:
    """Parse one worker draft into a candidate list.

    Raises:
        JudgeParseError: not JSON, missing a required field, or a chapter mismatch.
    """
    data = parse_judge_json(raw, REQUIRED_FIELDS)
    echoed = str(data.get("chapter_id") or "").strip()
    if echoed and echoed != chapter_id:
        raise JudgeParseError(
            f"chapter mismatch: draft echoed {echoed!r}, expected {chapter_id!r}"
        )
    candidates = data.get("candidates")
    if candidates is None:
        candidates = []
    if not isinstance(candidates, list):
        raise JudgeParseError(
            f"'candidates' must be a list, got {type(candidates).__name__}"
        )
    return [c for c in candidates if isinstance(c, dict)]


_COMMIT_SCHEMA = {
    "status": "'ok' | 'partial' (some chapters in failed/missing; the rest landed) | "
    "'error' (nothing parsed — candidates.json left as it was)",
    "counts": "{chapters, usable, unusable, failed, missing}",
    "by_chapter": "{chapter_id: usable candidate count}",
    "by_category": "{category: usable candidate count}",
    "failed": "list of {chapter_id, problem} — re-run the wave for these",
    "missing": "chapter ids with no draft on disk — re-run the wave",
    "unusable": "candidates refused before you saw them: {chapter_id, es_idx, "
    "quoted_span, reason}. The claim and rationale stay in candidates.json and the "
    "report; 'span_not_in_sentence' is the one to watch — the worker paraphrased",
    "candidates_path": "candidates.json — the shortlist Gate 2 cuts from",
    "report_path": "the dated markdown candidate report — READ THIS to relay the scan",
    "candidates": "not on stdout — the per-candidate text is in report_path and candidates_path",
    "instructions": "what to relay and what to re-run",
}

# Why a candidate was refused before the user saw it.
UNUSABLE_NO_ROW = "no_aligned_sentence"
UNUSABLE_SPAN = "span_not_in_sentence"
UNUSABLE_FIELDS = "missing_fields"
UNUSABLE_ALREADY = "already_noted"
UNUSABLE_DUPLICATE = "duplicate_span"


def scan_commit(
    project_dir: Path, *, report: bool = True
) -> dict[str, Any]:
    """Parse drafts, validate every candidate, write candidates.json + a report.

    Validation is the point: a candidate whose ``es_idx`` has no alignment row, or
    whose ``quoted_span`` does not occur in that aligned sentence, cannot become an
    anchor — so it is reported as ``unusable`` and never offered as a choice. The
    alternative is spending research on a hallucinated span and discovering it at
    ``add`` time.

    It is also where the English rejoins the work. Every usable candidate is
    stamped with ``en_sentence`` from the alignment — unconditionally, whatever
    the scanner was shown — which is what makes the default Spanish-only scan
    safe: the claim is checked against the source at Gate 2, by a reader who can
    act on the difference. See :func:`render_body`.
    """
    from src.footnote_pass import ledger as fp_ledger
    from src.footnote_pass.report import write_candidate_report

    project_dir = Path(project_dir)
    manifest_path = _manifest_path(project_dir)
    if not manifest_path.exists():
        return {
            "status": "error",
            "error": "no footnote scan manifest — run `scan-prepare` first",
            "_schema": _COMMIT_SCHEMA,
        }
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "status": "error",
            "error": f"unreadable manifest {manifest_path}: {exc}",
            "_schema": _COMMIT_SCHEMA,
        }

    entries = doc.get("entries") or []
    if not isinstance(entries, list):
        return {
            "status": "error",
            "error": f"manifest 'entries' is not a list: {type(entries).__name__}",
            "_schema": _COMMIT_SCHEMA,
        }

    fdir = footnotes_dir(project_dir).resolve()
    already = already_noted_keys(project_dir)

    usable: list[dict[str, Any]] = []
    unusable: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    missing: list[str] = []
    parsed = 0
    seen_keys: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("chapter_id"):
            failed.append({"chapter_id": "?", "problem": "malformed manifest entry"})
            continue
        chapter_id = entry["chapter_id"]
        draft_raw = entry.get("draft_path")
        if not draft_raw:
            failed.append({"chapter_id": chapter_id, "problem": "missing draft_path"})
            continue
        draft_path = Path(draft_raw)
        try:
            escaped = not draft_path.resolve().is_relative_to(fdir)
        except (OSError, RuntimeError, ValueError) as exc:
            failed.append(
                {
                    "chapter_id": chapter_id,
                    "problem": (
                        f"unresolvable draft_path: {type(exc).__name__}: {exc}"[:500]
                    ),
                }
            )
            continue
        if escaped:
            failed.append(
                {
                    "chapter_id": chapter_id,
                    "problem": f"draft_path escapes footnotes dir: {draft_path}",
                }
            )
            continue
        if not draft_path.exists():
            missing.append(chapter_id)
            continue
        try:
            raw = draft_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failed.append({"chapter_id": chapter_id, "problem": f"unreadable draft: {exc}"})
            continue
        try:
            candidates = parse_scan_draft(raw, chapter_id=chapter_id)
        except JudgeParseError as exc:
            failed.append({"chapter_id": chapter_id, "problem": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 - one bad draft must not sink the run
            logger.error("footnote scan-commit: %s parse crashed: %s", chapter_id, exc)
            failed.append(
                {"chapter_id": chapter_id, "problem": f"{type(exc).__name__}: {exc}"}
            )
            continue
        parsed += 1

        rows = load_alignment_rows(project_dir, chapter_id)
        es_map = {r.get("es_idx"): (r.get("es") or "") for r in rows}
        en_map = {r.get("es_idx"): (r.get("en") or "") for r in rows}

        for candidate in candidates:
            row = {
                "chapter_id": chapter_id,
                "es_idx": candidate.get("es_idx"),
                "quoted_span": str(candidate.get("quoted_span") or "").strip(),
                "category": str(candidate.get("category") or ""),
                "claim": str(candidate.get("claim") or ""),
                "why": str(candidate.get("why") or ""),
            }
            blank = [f for f in CANDIDATE_FIELDS if not row.get(f) and row.get(f) != 0]
            if blank:
                unusable.append({**row, "reason": UNUSABLE_FIELDS, "detail": f"blank: {blank}"})
                continue
            try:
                es_idx = int(row["es_idx"])
            except (TypeError, ValueError):
                unusable.append(
                    {
                        **row,
                        "reason": UNUSABLE_NO_ROW,
                        "detail": f"es_idx is not an integer: {row['es_idx']!r}",
                    }
                )
                continue
            row["es_idx"] = es_idx
            es_text = es_map.get(es_idx)
            if not es_text:
                unusable.append(
                    {
                        **row,
                        "reason": UNUSABLE_NO_ROW,
                        "detail": f"no alignment row for es_idx={es_idx}",
                    }
                )
                continue
            if row["quoted_span"] not in es_text:
                unusable.append(
                    {
                        **row,
                        "reason": UNUSABLE_SPAN,
                        "detail": (
                            "quoted_span does not occur verbatim in the aligned "
                            "sentence, so it cannot be an anchor"
                        ),
                        "es_sentence": es_text,
                    }
                )
                continue
            if (chapter_id, es_idx) in already:
                unusable.append(
                    {**row, "reason": UNUSABLE_ALREADY, "detail": "this sentence already has a footnote"}
                )
                continue
            # The stable id for this candidate, so a decision row can join back
            # to it exactly. `(chapter_id, es_idx)` alone is neither unique nor
            # stable — see `ledger.candidate_key`.
            key = fp_ledger.candidate_key(chapter_id, es_idx, row["quoted_span"])
            if key and key in seen_keys:
                # A second row under one key is unaddressable: no decision can
                # name it apart from the first, so it would sit in `undecided`
                # for good. Listed rather than dropped — its claim may differ.
                unusable.append(
                    {
                        **row,
                        "reason": UNUSABLE_DUPLICATE,
                        "detail": "the wave already proposed this span on this sentence",
                    }
                )
                continue
            if key:
                seen_keys.add(key)
            usable.append(
                {
                    **row,
                    "candidate_key": key,
                    "es_sentence": es_text,
                    # Attached whatever the scanner read. Under the default
                    # Spanish-only scan this is the *only* place the source
                    # reaches the decision, so it is not conditional on anything.
                    "en_sentence": en_map.get(es_idx, ""),
                }
            )

    counts = {
        "chapters": len(entries),
        "usable": len(usable),
        "unusable": len(unusable),
        "failed": len(failed),
        "missing": len(missing),
    }
    if not parsed and (failed or missing):
        # Nothing parsed, so there is nothing to replace the last shortlist with.
        # Writing anyway would empty the only file `add` joins against and report
        # `ok` over a wave that never ran. An empty-but-parsed scan still lands.
        return {
            "status": "error",
            "error": "no scan draft parsed — candidates.json left as it was",
            "counts": counts,
            "failed": failed,
            "missing": missing,
            "instructions": (
                "Nothing was replaced. Re-run `scan-fanout` (with --target-ids for "
                "these chapters), confirm drafts exist, then commit again."
            ),
            "_schema": _COMMIT_SCHEMA,
        }

    by_chapter: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for row in usable:
        by_chapter[row["chapter_id"]] = by_chapter.get(row["chapter_id"], 0) + 1
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1

    doc_out = {
        "project": project_dir.name,
        "committed_at": datetime.now().isoformat(),
        "profile_path": doc.get("profile_path"),
        "chapters": doc.get("chapters"),
        "worker_model": doc.get("worker_model"),
        "prompt_version": doc.get("prompt_version"),
        "source_text": doc.get("source_text") or DEFAULT_SOURCE_TEXT,
        "candidates": usable,
        "unusable": unusable,
        "failed": failed,
        "missing": missing,
    }
    candidates_path = _candidates_path(project_dir)
    candidates_path.write_text(
        json.dumps(doc_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    stamp = fp_ledger.run_id()
    report_path = (
        write_candidate_report(project_dir, doc_out, stamp=stamp) if report else None
    )

    # One `proposed` ledger row per usable candidate, so the ledger can answer
    # "what was proposed, and what became of it" on its own after the next
    # commit has replaced `candidates.json`. Not governed by `--no-report`: that
    # flag is about markdown, not about the record.
    try:
        fp_ledger.append_decisions(
            project_dir,
            [
                fp_ledger.proposed_row(project_dir, row, doc_out, stamp=stamp)
                for row in usable
            ],
        )
    except OSError as exc:
        logger.warning("footnote scan-commit: could not append to the ledger: %s", exc)

    return {
        "status": "partial" if failed or missing else "ok",
        "counts": counts,
        "by_chapter": by_chapter,
        "by_category": by_category,
        "failed": failed,
        "missing": missing,
        # Enough to see *what* was refused and spot the worst pattern — a
        # paraphrased span — without echoing the claim and rationale the report
        # and candidates.json already hold verbatim.
        "unusable": [
            {
                "chapter_id": row["chapter_id"],
                "es_idx": row["es_idx"],
                "quoted_span": row["quoted_span"],
                "reason": row["reason"],
            }
            for row in unusable
        ],
        "candidates_path": str(candidates_path),
        "report_path": str(report_path) if report_path else None,
        "instructions": (
            "Read report_path and relay it — the per-candidate text is there, not "
            "on stdout. Then take the shortlist to the user as Gate 2 and cut it "
            "BEFORE spending research on it. Re-run `scan-fanout --target-ids` for "
            "anything in failed/missing."
        ),
        "_schema": _COMMIT_SCHEMA,
    }
