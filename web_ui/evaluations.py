"""
Per-chunk evaluator persistence helpers for the Flask dashboard.

Results for each chunk are written to
``projects/<project_id>/evaluations/<chunk_id>.json`` (one file per chunk,
single source of truth — overwritten on each rerun). User feedback on
individual issues is appended to ``_feedback.jsonl`` in the same directory.

The module intentionally has no Flask or request-global dependencies so it
can be unit-tested against a bare ``tmp_path`` directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import is_dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional

from src.app_config import get_enabled_evaluators, get_blacklist_path
from src.evaluators import aggregate_results, run_all_evaluators
from src.evaluators.location_normalizer import NormalizedIssue, fan_out_issues
from src.models import (
    Blacklist,
    Chunk,
    EvalResult,
    EvaluationConfig,
    Glossary,
    IgnoredTerms,
)
from src.utils.file_io import load_blacklist, load_glossary, load_ignored_terms
from src.utils.text_utils import normalize_newlines

logger = logging.getLogger(__name__)

_FEEDBACK_FILENAME = "_feedback.jsonl"
_ALLOWED_FEEDBACK_TYPES = frozenset(
    {"false_positive", "bad_message", "missing_context_gap", "resolved"}
)

# Feedback records used to be keyed only by ``(eval_name, issue_index)`` — a
# POSITION in the evaluator's issue list, recomputed by ``enumerate`` at read
# time. Re-running an evaluator rewrites that list wholesale, so a mark silently
# re-pointed at whatever finding now occupied the slot; 87 marks in the local
# corpus already pointed past the end of their list when this was found, and any
# that landed on a *different* issue were undetectable. ``issue_key`` replaces the position with
# the finding's content, so a mark survives a re-run and stops meaning something
# it never meant.
#
# The separator is a unit separator rather than a common character so that
# content cannot forge a field boundary and collide with another finding.
_KEY_FIELD_SEP = "\x1f"
_KEY_LENGTH = 16


def issue_key(eval_name: str, issue: dict[str, Any]) -> str:
    """Stable content hash for one finding.

    Accepts either a raw evaluator/judge issue (``location`` is a string) or a
    ``normalized_issues`` entry (``location`` is a dict whose ``raw`` holds that
    same string), so the key computed when a mark is written matches the one
    computed when the badge counts are read.
    """
    location = issue.get("location")
    if isinstance(location, dict):
        location = location.get("raw")
    parts = [
        eval_name or "",
        str(issue.get("severity") or ""),
        str(issue.get("message") or ""),
        str(location or ""),
    ]
    digest = hashlib.sha256(_KEY_FIELD_SEP.join(parts).encode("utf-8")).hexdigest()
    return digest[:_KEY_LENGTH]


def build_dismissed(
    feedback_records: Iterable[dict[str, Any]],
) -> tuple[set[tuple[str, str]], set[tuple[str, Any]]]:
    """Split feedback into content-keyed and legacy position-keyed lookups.

    Returns ``(by_key, by_index)``. Records written before ``issue_key`` existed
    have no key and can only be matched positionally, so they land in the second
    set; everything written since matches on content. Pass both to
    :func:`is_dismissed`.
    """
    by_key: set[tuple[str, str]] = set()
    by_index: set[tuple[str, Any]] = set()
    for fb in feedback_records:
        eval_name = fb.get("eval_name")
        key = fb.get("issue_key")
        if key:
            by_key.add((eval_name, key))
        else:
            by_index.add((eval_name, fb.get("issue_index")))
    return by_key, by_index


def is_dismissed(
    by_key: set[tuple[str, str]],
    by_index: set[tuple[str, Any]],
    eval_name: str,
    issue_index: Any,
    issue: Optional[dict[str, Any]] = None,
) -> bool:
    """True if this finding carries any feedback label.

    All four feedback types count as dismissal — the distinction between them is
    tuning signal, not display state.
    """
    if issue is not None and (eval_name, issue_key(eval_name, issue)) in by_key:
        return True
    return (eval_name, issue_index) in by_index


# ``Issue.term`` is newer than the persisted corpus: every evaluation written
# before it existed has ``term: null``, which would make the ignore list inert
# on ~20 books until each was re-evaluated. The dictionary evaluator has always
# opened its message with the flagged word in single quotes
# (``'Sigfridos': Unknown word...``) -- the same convention
# ``location_normalizer._resolve_match_length`` already parses to size its
# highlight -- so the word is recoverable for exactly the findings that need it.
#
# Scoped to ``dictionary`` on purpose. ``blacklist`` shares the message shape
# but is not ignorable, and ``grammar``'s message is LanguageTool's localized
# Spanish prose, which never carries the token; grammar keeps needing a re-run
# to gain a ``rule_id`` regardless, and without one it is never suppressed.
_QUOTED_TERM_RE = re.compile(r"^'([^']+)'")


def issue_term(eval_name: str, issue: dict[str, Any]) -> Optional[str]:
    """The surface form a finding is about, preferring the stored field."""
    term = issue.get("term")
    if term:
        return term
    if eval_name == "dictionary":
        m = _QUOTED_TERM_RE.match(str(issue.get("message") or ""))
        if m:
            return m.group(1)
    return None


def is_ignored(
    ignored: Optional[IgnoredTerms],
    eval_name: str,
    issue: Optional[dict[str, Any]],
) -> bool:
    """True if this finding names a term the book has put on its ignore list.

    A *sibling* of :func:`is_dismissed`, not a replacement. A dismissal is one
    human judgment about one finding; an ignore is one judgment about a term,
    applied to every finding that names it, book-wide.

    Deliberately filtered here at read time rather than suppressed inside the
    evaluator (which is what putting the word in the glossary does):

    - Add and remove stay symmetric. Evaluate-time suppression can only be
      undone by another full rerun, because the finding is no longer in the
      stored evaluation to bring back.
    - ``evaluations/<chunk_id>.json`` keeps recording what the checker actually
      found, so the per-rule precision arithmetic stays measurable.
    - It is free: ``dictionary`` is a local enchant lookup, not an API call.

    The cost, matching how dismissals already behave: the evaluator's own
    ``score`` / ``passed`` / ``metadata`` still count ignored words. Only the
    finding counts and lists respond.
    """
    if not ignored or not ignored.terms or not issue:
        return False
    return ignored.matches(eval_name, issue_term(eval_name, issue), issue.get("rule_id"))


# ---------------------------------------------------------------------------
# Paths


def _eval_results_dir(project_dir: Path) -> Path:
    """Return ``projects/<id>/evaluations/`` (does not create it)."""
    return Path(project_dir) / "evaluations"


def _eval_file(project_dir: Path, chunk_id: str) -> Path:
    """Return the path to a chunk's evaluation JSON file."""
    return _eval_results_dir(project_dir) / f"{chunk_id}.json"


def _feedback_file(project_dir: Path) -> Path:
    """Return the path to the per-project feedback JSONL file."""
    return _eval_results_dir(project_dir) / _FEEDBACK_FILENAME


# ---------------------------------------------------------------------------
# Serialization helpers


def _serialize_result(result: EvalResult) -> dict[str, Any]:
    """Dump an :class:`EvalResult` to a JSON-safe dict."""
    try:
        return result.model_dump(mode="json")
    except Exception:
        # Defensive fallback for older pydantic versions.
        return json.loads(result.model_dump_json())


def _rehydrate_result(payload: dict[str, Any]) -> Optional[EvalResult]:
    """Rebuild an :class:`EvalResult` from a previously persisted dict.

    Used when a narrowed rerun carries an untouched evaluator's results
    forward. Returns ``None`` if the stored shape no longer validates (an old
    file written before a model change), in which case that evaluator's
    findings are simply dropped rather than crashing the rerun.
    """
    try:
        return EvalResult.model_validate(payload)
    except Exception as e:  # noqa: BLE001 - a stale on-disk shape is not fatal
        logger.debug("Could not rehydrate persisted eval result: %s", e)
        return None


def _serialize_issue(issue: NormalizedIssue | dict[str, Any]) -> dict[str, Any]:
    """Accept either a ``NormalizedIssue`` or a plain dict and return a dict."""
    if isinstance(issue, NormalizedIssue):
        return issue.to_dict()
    if is_dataclass(issue):
        return asdict(issue)
    return dict(issue)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically via a temp file rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Per-evaluator freshness ledger
#
# ``evaluations/<chunk>.json`` carries an ``eval_runs`` block:
#
#     "eval_runs": {"grammar": {"at": "<iso>", "text_sha": "<sha256>"}, ...}
#
# recording, per evaluator/judge, the hash of the ``translated_text`` it
# actually judged. Every path that rewrites a chunk (the chunk editor,
# /api/correction, /api/apply-corrections, /api/sentence/replace, a judge
# apply) changes that hash, so a persisted verdict can no longer assert itself
# against prose it never saw — without any of those paths having to remember to
# stamp a flag.


def chunk_text_sha(text: str) -> str:
    """Content hash of a chunk translation, newline-normalized first.

    Normalizing means a CRLF/LF round-trip through an editor is not mistaken
    for an edit — the same normalization the reader's review anchoring applies
    before searching chunk text.
    """
    return hashlib.sha256(normalize_newlines(text or "").encode("utf-8")).hexdigest()


def current_chunk_sha(project_dir: Path, chunk_id: str) -> Optional[str]:
    """Hash the chunk's translation as it currently sits on disk.

    Returns ``None`` when the chunk file is missing or unreadable — callers
    treat that as "cannot judge freshness" rather than "stale", so a project
    whose chunks moved does not paint every badge red.
    """
    path = Path(project_dir) / "chunks" / f"{chunk_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return chunk_text_sha(data.get("translated_text") or "")


def _entry_predates(entry: Optional[dict[str, Any]], since: Optional[str]) -> bool:
    """Was this ledger entry recorded before ``since`` (an ISO stamp)?

    An evaluator with no entry, or one whose ``at`` will not parse, counts as
    predating: no evidence that it re-ran is not evidence that it did. Used to
    scope the chunk-level ``stale`` flag to the evaluators it still describes.
    """
    ran_at = entry.get("at") if isinstance(entry, dict) else None
    if not (since and ran_at):
        return True
    try:
        return datetime.fromisoformat(str(ran_at)) < datetime.fromisoformat(str(since))
    except ValueError:
        return True


def _stamp_eval_runs(
    previous: Optional[dict[str, Any]],
    names: Iterable[str],
    text_sha: Optional[str],
    *,
    keep: Optional[Iterable[str]] = None,
) -> Optional[dict[str, Any]]:
    """Merge a run of ``names`` into the previous ledger.

    ``keep`` names the evaluators whose *findings* survive into the payload
    being written, so their prior ledger entry is carried over untouched — a
    coded rerun must not silently claim the dialogue judge is current, and it
    must not erase the judge's ledger either. Everything else is dropped: an
    evaluator whose results are no longer in the file has no run to record.
    ``None`` keeps the whole prior ledger (a patch-in-place write).
    """
    prior: dict[str, Any] = {}
    if isinstance(previous, dict):
        raw = previous.get("eval_runs")
        if isinstance(raw, dict):
            prior = {k: v for k, v in raw.items() if isinstance(v, dict)}

    if keep is None:
        ledger = dict(prior)
    else:
        keep_set = set(keep)
        ledger = {k: v for k, v in prior.items() if k in keep_set}

    now = datetime.now().isoformat()
    for name in [n for n in names if n]:
        entry: dict[str, Any] = {"at": now}
        if text_sha is not None:
            entry["text_sha"] = text_sha
        ledger[name] = entry
    return ledger or None


def evaluator_freshness_detail(
    payload: Optional[dict[str, Any]],
    current_sha: Optional[str],
    *,
    names: Optional[Iterable[str]] = None,
    chunk_mtime: Optional[float] = None,
) -> dict[str, dict[str, Optional[str]]]:
    """Return ``{evaluator_name: {"state": ..., "basis": ...}}``.

    ``state`` is ``"fresh" | "stale" | "missing"``; ``basis`` names *which*
    source decided it — ``"flag"``, ``"hash"``, ``"mtime"``, or ``None`` for a
    ``missing`` verdict (and for a recorded run whose freshness nothing could
    disprove).

    The basis is not decoration. Only a minority of the evaluation files on
    disk carry an ``eval_runs`` ledger — every project evaluated before it
    existed falls through to the timestamp rule, where a git checkout, a
    byte-identical rewrite or a re-chunk all bump the mtime. ``"mtime"``-stale
    is a *suspicion*; ``"hash"``- and ``"flag"``-stale are facts, and a caller
    telling an operator to re-run N chapters should be able to say which it has.

    Args:
        payload: The chunk's persisted evaluation, or ``None`` if never run.
        current_sha: :func:`current_chunk_sha` for the chunk right now. ``None``
            (unreadable chunk) means freshness cannot be disproved, so recorded
            runs read ``fresh``.
        names: Evaluators the caller wants an answer for. Any name with no
            evidence in ``payload`` comes back ``"missing"``. When omitted,
            only the names the payload has evidence for are reported.
        chunk_mtime: Modification time of ``chunks/<chunk>.json``, used for the
            legacy fallback below.

    Freshness sources, in order:

    1. The top-level ``stale`` flag (written by a judge apply) still wins, so
       apply-fix behaviour is exactly what it was.
    2. ``eval_runs[name].text_sha`` vs ``current_sha``.
    3. **Legacy fallback** for evaluations written before ``eval_runs`` existed:
       an evaluator with no ledger entry but visible evidence it ran
       (``enabled_evals`` / ``judges``) is compared by timestamp — a chunk file
       newer than ``evaluated_at`` / ``judges_at`` means the text moved on.
       Those files self-heal into the ledger on the next rerun.
    """
    requested = list(names) if names is not None else None
    missing: dict[str, Optional[str]] = {"state": "missing", "basis": None}

    if not isinstance(payload, dict):
        return {name: dict(missing) for name in (requested or [])}

    ledger = payload.get("eval_runs")
    ledger = ledger if isinstance(ledger, dict) else {}
    judges = payload.get("judges")
    judges = judges if isinstance(judges, dict) else {}
    ran_coded = payload.get("enabled_evals")
    ran_coded = [n for n in ran_coded if isinstance(n, str)] if isinstance(ran_coded, list) else []

    evidenced = set(ledger) | set(judges) | set(ran_coded)
    targets = requested if requested is not None else sorted(evidenced)

    explicitly_stale = bool(payload.get("stale"))

    def _flag_covers(entry: Optional[dict[str, Any]]) -> bool:
        """Does the chunk-level ``stale`` flag still describe this evaluator?

        The flag is written per *chunk* — "the text moved under the verdicts
        recorded before this edit" — but an evaluator whose ledger entry is
        dated at or after ``stale_since`` has re-run since, and carries its own
        newer evidence. Without this scoping, re-running one judge had to
        either leave every other judge wrongly stale or clear the flag
        outright, which laundered them all fresh.
        """
        return explicitly_stale and _entry_predates(entry, payload.get("stale_since"))

    def _legacy(name: str) -> tuple[str, Optional[str]]:
        """Timestamp comparison for a pre-``eval_runs`` evaluation."""
        if name in judges:
            stamp = payload.get("judges_at") or payload.get("evaluated_at")
        elif name in ran_coded:
            stamp = payload.get("evaluated_at")
        else:
            return "missing", None
        if chunk_mtime is None or not stamp:
            return "fresh", None
        try:
            ran_at = datetime.fromisoformat(str(stamp)).timestamp()
        except ValueError:
            return "fresh", None
        return ("stale", "mtime") if chunk_mtime > ran_at else ("fresh", "mtime")

    out: dict[str, dict[str, Optional[str]]] = {}
    for name in targets:
        entry = ledger.get(name)
        if isinstance(entry, dict) and entry.get("text_sha"):
            if _flag_covers(entry):
                out[name] = {"state": "stale", "basis": "flag"}
            elif current_sha is None:
                out[name] = {"state": "fresh", "basis": None}
            else:
                same = entry["text_sha"] == current_sha
                out[name] = {"state": "fresh" if same else "stale", "basis": "hash"}
            continue
        state, basis = _legacy(name)
        if state != "missing" and _flag_covers(None):
            state, basis = "stale", "flag"
        out[name] = {"state": state, "basis": basis if state != "missing" else None}
    return out


def evaluator_freshness(
    payload: Optional[dict[str, Any]],
    current_sha: Optional[str],
    *,
    names: Optional[Iterable[str]] = None,
    chunk_mtime: Optional[float] = None,
) -> dict[str, str]:
    """Return ``{evaluator_name: "fresh" | "stale" | "missing"}``.

    The state half of :func:`evaluator_freshness_detail` — see there for the
    argument meanings and the three-source precedence ladder. Kept as the
    hot-path shape most callers want (the dashboard's Review rollup asks this
    question once per chunk per page load and never looks at the basis).
    """
    return {
        name: str(entry["state"])
        for name, entry in evaluator_freshness_detail(
            payload, current_sha, names=names, chunk_mtime=chunk_mtime
        ).items()
    }


# ---------------------------------------------------------------------------
# Public API


def save_chunk_evaluation(
    project_dir: Path,
    chunk_id: str,
    results: Iterable[EvalResult],
    aggregated: dict[str, Any],
    normalized_issues: Iterable[NormalizedIssue | dict[str, Any]],
    *,
    enabled_evals: Optional[list[str]] = None,
    llm_judge: Optional[dict[str, Any]] = None,
    judges: Optional[dict[str, Any]] = None,
    stale_mark: Optional[dict[str, str]] = None,
    text_sha: Optional[str] = None,
    stamp_evals: Optional[Iterable[str]] = None,
) -> Path:
    """Persist a full evaluation run for ``chunk_id``.

    Overwrites any previous file — callers use :func:`merge_llm_judge_result`
    and :func:`merge_judge_result` to tack on the LLM judge / tailored-judge
    sections without losing the coded results.

    Args:
        project_dir: ``projects/<id>/`` directory.
        chunk_id: Chunk identifier (already validated by caller).
        results: Iterable of per-evaluator results.
        aggregated: Output from :func:`aggregate_results`.
        normalized_issues: Flattened view-layer issues for the UI.
        enabled_evals: Optional list of evaluator names the run enabled. If
            ``None``, inferred from the ``results`` list in order.
        llm_judge: Optional existing llm_judge section to preserve when the
            caller is replacing the coded evaluation.
        judges: Optional existing tailored-judge section (``{name: result}``)
            to preserve when the caller is replacing the coded evaluation.
        stale_mark: When preserving outdated judge findings after a text edit,
            pass ``{"stale_since": ..., "stale_reason": ...}`` to re-stamp the
            evaluation as stale (see :func:`mark_evaluation_stale`).
        text_sha: Content hash of the ``translated_text`` these results describe.
            Defaults to hashing ``chunks/<chunk_id>.json`` on disk, so callers
            that already hold the chunk can skip a read and callers that don't
            still get a stamped ledger.
        stamp_evals: Which of ``enabled_evals`` actually ran against
            ``text_sha`` just now. Defaults to all of them. A narrowed rerun
            passes the smaller list so evaluators whose *previous* findings are
            being carried forward keep their original — possibly stale — hash.

    Returns:
        Path to the written JSON file.
    """
    results_list = list(results)
    serialized_results = [_serialize_result(r) for r in results_list]

    if enabled_evals is None:
        enabled_evals = [r.eval_name for r in results_list]
    if text_sha is None:
        text_sha = current_chunk_sha(project_dir, chunk_id)

    # Read the file we are about to overwrite so the ledger keeps the entries
    # for evaluators this run did not touch (the tailored judges, above all).
    previous = load_chunk_evaluation(project_dir, chunk_id)

    payload: dict[str, Any] = {
        "chunk_id": chunk_id,
        "evaluated_at": datetime.now().isoformat(),
        "enabled_evals": enabled_evals,
        "aggregated": aggregated,
        "results": serialized_results,
        "normalized_issues": [_serialize_issue(i) for i in normalized_issues],
        "llm_judge": llm_judge,
        "judges": judges,
        # The ledger describes exactly what this file now holds: the evaluators
        # whose results are in it, plus the judges carried over. A wipe of
        # ``judges`` takes their ledger rows with it.
        "eval_runs": _stamp_eval_runs(
            previous,
            enabled_evals if stamp_evals is None else stamp_evals,
            text_sha,
            keep=list(enabled_evals) + (list(judges) if isinstance(judges, dict) else []),
        ),
    }
    # Carrying a block over means carrying over *when it ran*. Dropping these
    # would relabel a judge verdict inherited from an earlier run with this
    # run's timestamp — which, for an evaluation written before ``eval_runs``
    # existed, is exactly what would launder a stale verdict into a fresh one.
    if isinstance(previous, dict):
        if judges and previous.get("judges_at"):
            payload["judges_at"] = previous["judges_at"]
        if llm_judge is not None and previous.get("llm_judge_at"):
            payload["llm_judge_at"] = previous["llm_judge_at"]
    if stale_mark:
        payload["stale"] = True
        payload["stale_since"] = stale_mark.get("stale_since") or datetime.now().isoformat()
        payload["stale_reason"] = stale_mark.get("stale_reason") or (
            "Persisted judge findings may not match the current translation."
        )

    path = _eval_file(project_dir, chunk_id)
    _atomic_write_json(path, payload)
    logger.debug("Saved chunk evaluation: %s", path)
    return path


def load_chunk_evaluation(
    project_dir: Path, chunk_id: str
) -> Optional[dict[str, Any]]:
    """Load the saved evaluation for a chunk, or ``None`` if missing."""
    path = _eval_file(project_dir, chunk_id)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load evaluation %s: %s", path, e)
        return None


def merge_llm_judge_result(
    project_dir: Path,
    chunk_id: str,
    result: dict[str, Any],
) -> Path:
    """Patch the ``llm_judge`` key of a chunk's evaluation JSON.

    Creates a minimal shell file if no coded evaluation exists yet — that way
    the LLM judge button still works in isolation.
    """
    payload = load_chunk_evaluation(project_dir, chunk_id)
    if payload is None:
        payload = {
            "chunk_id": chunk_id,
            "evaluated_at": datetime.now().isoformat(),
            "enabled_evals": [],
            "aggregated": None,
            "results": [],
            "normalized_issues": [],
            "llm_judge": None,
        }
    payload["llm_judge"] = result
    payload["llm_judge_at"] = datetime.now().isoformat()

    path = _eval_file(project_dir, chunk_id)
    _atomic_write_json(path, payload)
    return path


def merge_judge_result(
    project_dir: Path,
    chunk_id: str,
    judge_name: str,
    result: dict[str, Any],
) -> Path:
    """Patch one tailored judge's entry in a chunk's ``judges`` section.

    Stores ``result`` (a serialized :class:`EvalResult`) under
    ``payload["judges"][judge_name]`` without disturbing coded results or the
    quality ``llm_judge`` block. Creates a minimal shell file if no evaluation
    exists yet, so a judge can run before the coded evaluators have.
    """
    payload = load_chunk_evaluation(project_dir, chunk_id)
    if payload is None:
        payload = {
            "chunk_id": chunk_id,
            "evaluated_at": datetime.now().isoformat(),
            "enabled_evals": [],
            "aggregated": None,
            "results": [],
            "normalized_issues": [],
            "llm_judge": None,
            "judges": None,
        }

    judges = payload.get("judges")
    if not isinstance(judges, dict):
        judges = {}
    judges[judge_name] = result
    payload["judges"] = judges
    # ``judges_at`` is the *legacy* per-chunk stamp: it dates judges that have
    # no ledger entry. This judge is getting one below, so bumping the shared
    # stamp would only re-date the others — turning a pre-``eval_runs`` verdict
    # that never saw the current prose green. Same reasoning as the carry-over
    # in `save_chunk_evaluation`; set it only when there is nothing to keep.
    payload.setdefault("judges_at", datetime.now().isoformat())
    # Single persistence seam for both judge backends (API and subagent), so a
    # CLI run and a dashboard run stamp the freshness ledger identically.
    current = current_chunk_sha(project_dir, chunk_id)
    payload["eval_runs"] = _stamp_eval_runs(payload, [judge_name], current)
    # This judge's fresh run outdates the stale marker for *itself* — its new
    # ledger entry postdates ``stale_since``, which is what scopes the flag.
    # Drop the flag only once no other evaluator is still covered by it;
    # clearing it wholesale relabelled their older verdicts as current.
    runs = payload.get("eval_runs")
    runs = runs if isinstance(runs, dict) else {}
    coded = payload.get("enabled_evals")
    coded = [n for n in coded if isinstance(n, str)] if isinstance(coded, list) else []
    others = (set(judges) | set(coded)) - {judge_name}
    since = payload.get("stale_since")
    if not any(_entry_predates(runs.get(name), since) for name in others):
        for key in ("stale", "stale_since", "stale_reason"):
            payload.pop(key, None)

    path = _eval_file(project_dir, chunk_id)
    _atomic_write_json(path, payload)
    return path


def mark_evaluation_stale(
    project_dir: Path, chunk_id: str, reason: str,
) -> Optional[Path]:
    """Flag a chunk's persisted evaluation as stale after its text changed.

    Applying a judge fix rewrites ``translated_text``, so the findings persisted
    in ``evaluations/<chunk>.json`` no longer describe the current translation.
    We stamp ``stale``/``stale_since``/``stale_reason`` rather than delete the
    file so a green (or failing) badge never silently outlives the edit that
    invalidated it. Re-running the judge (:func:`merge_judge_result`) clears the
    marker.

    Returns the written path, or ``None`` if no evaluation exists yet (nothing
    to invalidate).
    """
    payload = load_chunk_evaluation(project_dir, chunk_id)
    if payload is None:
        return None
    now = datetime.now().isoformat()
    payload["stale"] = True
    payload["stale_since"] = now
    payload["stale_reason"] = reason

    path = _eval_file(project_dir, chunk_id)
    _atomic_write_json(path, payload)
    return path


def append_feedback(
    project_dir: Path,
    chunk_id: str,
    eval_name: str,
    issue_index: int,
    feedback_type: str,
    message: Optional[str] = None,
    note: Optional[str] = None,
    key: Optional[str] = None,
) -> Path:
    """Append a single feedback record to ``_feedback.jsonl``.

    ``key`` is the :func:`issue_key` content hash of the finding being marked.
    Callers should always supply it; ``issue_index`` is still recorded, but only
    as provenance, because it stops being meaningful the moment the evaluator
    re-runs.

    Raises:
        ValueError: If ``feedback_type`` is not one of the allowed labels.
    """
    if feedback_type not in _ALLOWED_FEEDBACK_TYPES:
        raise ValueError(
            f"Unknown feedback_type {feedback_type!r}; "
            f"expected one of {sorted(_ALLOWED_FEEDBACK_TYPES)}"
        )

    record = {
        "ts": datetime.now().isoformat(),
        "chunk_id": chunk_id,
        "eval_name": eval_name,
        "issue_index": issue_index,
        "issue_key": key,
        "feedback_type": feedback_type,
        "message": message,
        "note": note,
    }

    path = _feedback_file(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def load_feedback_for_chunk(
    project_dir: Path, chunk_id: str
) -> list[dict[str, Any]]:
    """Return all feedback records for ``chunk_id`` from ``_feedback.jsonl``.

    Records are returned in insertion order. Since feedback is append-only,
    multiple records for the same ``(eval_name, issue_index)`` key may be
    present — callers that want "the latest label" should iterate and keep
    the last match per key.

    Malformed lines and I/O errors are swallowed with a log (feedback is
    best-effort UI state, not authoritative).
    """
    path = _feedback_file(project_dir)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.debug(
                        "Skipping malformed feedback line in %s: %s", path, e
                    )
                    continue
                if record.get("chunk_id") == chunk_id:
                    out.append(record)
    except OSError as e:
        logger.warning("Failed to read feedback file %s: %s", path, e)
    return out


def load_all_feedback_by_chunk(
    project_dir: Path,
) -> dict[str, list[dict[str, Any]]]:
    """Return all feedback records grouped by ``chunk_id``.

    Reads ``_feedback.jsonl`` once — use for chapter-wide review assembly
    instead of calling :func:`load_feedback_for_chunk` per chunk.
    """
    path = _feedback_file(project_dir)
    if not path.exists():
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.debug(
                        "Skipping malformed feedback line in %s: %s", path, e
                    )
                    continue
                chunk_id = record.get("chunk_id")
                if not chunk_id:
                    continue
                out.setdefault(chunk_id, []).append(record)
    except OSError as e:
        logger.warning("Failed to read feedback file %s: %s", path, e)
    return out


def load_project_summary(project_dir: Path) -> dict[str, dict[str, int]]:
    """Walk ``evaluations/*.json`` and return a per-chunk counts map.

    The shape matches what the chapter-table badge renderer expects:

    ``{chunk_id: {"errors": int, "warnings": int, "info": int, "total": int,
    "stale": int (optional, 1 when findings are invalidated)}}``.

    Missing or malformed files are skipped with a debug log — the summary is
    best-effort. Stale evaluations (text edited after the run) contribute
    ``stale: 1`` and zero severity counts so chapter badges do not show
    outdated judge/coded findings as current.
    """
    out: dict[str, dict[str, int]] = {}
    eval_dir = _eval_results_dir(project_dir)
    if not eval_dir.exists():
        return out

    for path in eval_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Skipping unreadable evaluation %s: %s", path, e)
            continue

        chunk_id = data.get("chunk_id") or path.stem
        if data.get("stale"):
            out[chunk_id] = {
                "errors": 0,
                "warnings": 0,
                "info": 0,
                "total": 0,
                "stale": 1,
            }
            continue

        aggregated = data.get("aggregated") or {}
        severity = aggregated.get("issues_by_severity") or {}
        errors = int(severity.get("error", 0) or 0)
        warnings = int(severity.get("warning", 0) or 0)
        info = int(severity.get("info", 0) or 0)
        total = int(aggregated.get("total_issues", 0) or 0)

        # Fold tailored-judge findings into the same badge counts so a chunk
        # judged but not coded-evaluated still lights up (and vice versa).
        judges = data.get("judges")
        if isinstance(judges, dict):
            for judge_result in judges.values():
                if not isinstance(judge_result, dict):
                    continue
                for issue in judge_result.get("issues") or []:
                    sev = (issue or {}).get("severity")
                    if sev == "error":
                        errors += 1
                    elif sev == "warning":
                        warnings += 1
                    elif sev == "info":
                        info += 1
                    total += 1

        out[chunk_id] = {
            "errors": errors,
            "warnings": warnings,
            "info": info,
            "total": total,
        }

    return out


def chapter_id_from_chunk_id(chunk_id: str) -> str:
    """``'chapter_01_chunk_003'`` -> ``'chapter_01'``.

    A chunk_id with no ``_chunk_`` marker is returned unchanged rather than
    dropped: it will never match a chapter row, but its findings still have to
    reach the project-level total that :func:`load_project_type_counts` sums.
    """
    idx = chunk_id.rfind("_chunk_")
    return chunk_id[:idx] if idx > 0 else chunk_id


def load_chapter_type_counts(project_dir: Path) -> dict[str, dict[str, int]]:
    """Walk ``evaluations/*.json`` and count live findings per chapter, per category.

    Same shape of walk as :func:`load_project_summary`, but bucketed by
    category rather than severity, and gated exactly the way the reader's
    Review Mode gates them (``web_ui/app.py:project_chapter_review``): stale
    chunks are skipped, dismissed findings (``_feedback.jsonl``) are subtracted,
    coded findings must be target-side with a ``char_start``, and only the six
    :data:`REVIEW_TYPES` categories are counted.

    A finding the reader cannot anchor to a sentence — a judge excerpt that
    has drifted from the prose, a ``char_start`` no row covers — is not
    dropped there any more: it is returned in ``unanchored`` and rendered in
    the end-of-chapter overflow bin, and counted.

    The chip can still exceed the bin when a chunk has evaluation results but
    no usable alignment rows (IMAGE-only, or ``_attach_text_in_chunk`` never
    set offsets): this walk counts them; ``project_chapter_review`` never
    visits that chunk. (What this walk still cannot say is *where* each one
    lands, which needs the alignments plus the chunk text — far too expensive
    for a list page.)

    Returns:
        ``{chapter_id: {category: count}}``, each inner dict holding all six
        :data:`REVIEW_TYPES` keys zero-filled. Chapters with no live findings
        are absent — callers wanting a row for every chapter should fall back
        to :func:`empty_type_counts`.
    """
    by_chapter: dict[str, dict[str, int]] = {}
    eval_dir = _eval_results_dir(project_dir)
    if not eval_dir.exists():
        return by_chapter

    coded_types = frozenset(REVIEW_CODED_TYPES)
    judge_types = frozenset(REVIEW_JUDGE_TYPES)
    feedback_by_chunk = load_all_feedback_by_chunk(project_dir)
    ignored = load_project_ignored_terms(project_dir)

    for path in sorted(eval_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Skipping unreadable evaluation %s: %s", path, e)
            continue
        if not isinstance(data, dict) or data.get("stale"):
            continue

        chunk_id = data.get("chunk_id") or path.stem
        fb_by_key, fb_by_index = build_dismissed(feedback_by_chunk.get(chunk_id, []))
        counts = by_chapter.setdefault(chapter_id_from_chunk_id(chunk_id), empty_type_counts())

        for ni in data.get("normalized_issues") or []:
            if not isinstance(ni, dict):
                continue
            eval_name = ni.get("eval_name")
            if eval_name not in coded_types:
                continue
            loc = ni.get("location") or {}
            if loc.get("side") != "target" or loc.get("char_start") is None:
                continue
            if is_dismissed(
                fb_by_key, fb_by_index, eval_name, ni.get("issue_index"), ni
            ):
                continue
            if is_ignored(ignored, eval_name, ni):
                continue
            counts[eval_name] += 1

        judges = data.get("judges")
        if not isinstance(judges, dict):
            continue
        for judge_name, jres in judges.items():
            if judge_name not in judge_types or not isinstance(jres, dict):
                continue
            for issue_index, issue in enumerate(jres.get("issues") or []):
                if not isinstance(issue, dict):
                    continue
                if is_dismissed(
                    fb_by_key, fb_by_index, judge_name, issue_index, issue
                ):
                    continue
                counts[judge_name] += 1

    return by_chapter


class IgnoreHits(NamedTuple):
    """What one ignore entry is holding down, split by who is holding it.

    ``live`` is what the entry alone suppresses -- exactly what comes back if
    the entry is cleared. ``dismissed`` is the findings that name the same term
    but already carry a feedback label, so the dismissal would keep them hidden
    either way and clearing the entry does not restore them.

    The split matters because a dismissal is keyed on the finding's message and
    raw location (:func:`issue_key`), so it stops matching the moment the chunk
    is edited and re-evaluated -- at which point the ignore entry becomes the
    only thing still suppressing those findings. ``live == 0`` therefore does
    not mean "inert"; only ``live == 0 and dismissed == 0`` does.
    """

    live: int = 0
    dismissed: int = 0


def count_ignored_hits(
    project_dir: Path, ignored: Optional[IgnoredTerms]
) -> dict[tuple[str, str, Optional[str]], IgnoreHits]:
    """How many findings each ignore entry covers, split live vs. dismissed.

    Keyed by :meth:`IgnoredTerm.identity`. "Live" means the same thing it means
    to the review badges: a target-anchored coded finding in a non-stale chunk
    that carries no feedback label. Only a ``live == 0 and dismissed == 0`` row
    is the signal that the list has outlived the text it was written against;
    a zero beside a non-zero ``dismissed`` means you dismissed those findings by
    hand before the term was ignored, and the dismissal simply got there first.

    Same walk as :func:`load_chapter_type_counts`; one pass over the project's
    evaluations, only paid for when the Review stage asks for it.
    """
    hits: dict[tuple[str, str, Optional[str]], IgnoreHits] = {}
    if not ignored or not ignored.terms:
        return hits

    # Pre-index the entries so each finding costs a dict lookup, not a scan.
    by_identity = {entry.identity(): entry for entry in ignored.terms}
    for identity in by_identity:
        hits[identity] = IgnoreHits()

    eval_dir = _eval_results_dir(project_dir)
    if not eval_dir.exists():
        return hits

    coded_types = frozenset(REVIEW_CODED_TYPES)
    feedback_by_chunk = load_all_feedback_by_chunk(project_dir)

    for path in sorted(eval_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Skipping unreadable evaluation %s: %s", path, e)
            continue
        if not isinstance(data, dict) or data.get("stale"):
            continue

        chunk_id = data.get("chunk_id") or path.stem
        fb_by_key, fb_by_index = build_dismissed(feedback_by_chunk.get(chunk_id, []))

        for ni in data.get("normalized_issues") or []:
            if not isinstance(ni, dict):
                continue
            eval_name = ni.get("eval_name")
            if eval_name not in coded_types:
                continue
            term = issue_term(eval_name, ni)
            if not term:
                continue
            loc = ni.get("location") or {}
            if loc.get("side") != "target" or loc.get("char_start") is None:
                continue
            # Mirror IgnoredTerms.matches: grammar is keyed on the pair and
            # is unsuppressable without a rule id; every other evaluator is
            # keyed on the word alone, so its rule slot is always None.
            rule_id = (ni.get("rule_id") or None) if eval_name == "grammar" else None
            if eval_name == "grammar" and not rule_id:
                continue
            identity = (eval_name, term.strip().casefold(), rule_id)
            current = hits.get(identity)
            if current is None:
                continue
            if is_dismissed(
                fb_by_key, fb_by_index, eval_name, ni.get("issue_index"), ni
            ):
                hits[identity] = current._replace(dismissed=current.dismissed + 1)
            else:
                hits[identity] = current._replace(live=current.live + 1)

    return hits


def load_project_type_counts(project_dir: Path) -> dict[str, int]:
    """Roll :func:`load_chapter_type_counts` up to one count per review category.

    Same walk, same gating, same upper-bound caveat — see that function.

    Returns:
        All six :data:`REVIEW_TYPES` keys, zero-filled.
    """
    totals = empty_type_counts()
    for counts in load_chapter_type_counts(project_dir).values():
        for name, n in counts.items():
            totals[name] += n
    return totals


# ---------------------------------------------------------------------------
# High-level evaluation runner


# The finding categories the reader's Review Mode can paint, and the home-page
# card can count. Single source of truth: ``web_ui/app.py`` builds its
# frozensets from these, the Jinja pickers loop over them, and ``reader.js``
# receives them as ``window.REVIEW_TYPES``.
#
# Coded evaluators whose target-side issues carry a highlightable char span.
REVIEW_CODED_TYPES: tuple[str, ...] = ("blacklist", "grammar", "dictionary", "completeness")
# Tailored judges whose issues can be anchored to a sentence by text search.
REVIEW_JUDGE_TYPES: tuple[str, ...] = ("dialogue", "address")
REVIEW_TYPES: tuple[str, ...] = REVIEW_CODED_TYPES + REVIEW_JUDGE_TYPES


def empty_type_counts() -> dict[str, int]:
    """A fresh zero-filled count per :data:`REVIEW_TYPES` category.

    Every category is always present so the template and the JS re-sum can
    index a card or a chapter row without guarding for missing keys.
    """
    return {name: 0 for name in REVIEW_TYPES}


# The seven coded evaluators; ``llm_judge`` is deliberately excluded here and
# exposed via its own opt-in endpoint.
CODED_EVAL_NAMES: tuple[str, ...] = (
    "length",
    "paragraph",
    "dictionary",
    "glossary",
    "completeness",
    "blacklist",
    "grammar",
)


# The Review tab's status cells. The deterministic evaluators always run as one
# set, so they get one cell; each tailored judge is run separately and gets its
# own. The quality ``llm_judge`` is deliberately absent — it stays on the
# Translate stage's per-chunk card.
JUDGE_STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "coded": CODED_EVAL_NAMES,
    "dialogue": ("dialogue",),
    "address": ("address",),
}


def chunk_group_states(
    freshness: dict[str, str],
    groups: Optional[dict[str, tuple[str, ...]]] = None,
) -> dict[str, str]:
    """Collapse one chunk's per-evaluator freshness into one state per group.

    ``coded`` reads only the evaluators the ledger actually recorded, never the
    full seven: ``app_config.json`` may narrow the set (see
    :func:`get_enabled_evaluators`), and demanding the full list there would
    pin a correctly-configured project at a permanent ``partial``. No coded
    entry at all is the only thing that means "never run".

    ``groups`` defaults to :data:`JUDGE_STATUS_GROUPS` (what the dashboard's
    Review tab shows). The CLI passes a wider map so a judge registered after
    that constant was written is not invisible.
    """
    out: dict[str, str] = {}
    for group, names in (groups or JUDGE_STATUS_GROUPS).items():
        states = [freshness[n] for n in names if freshness.get(n, "missing") != "missing"]
        if not states:
            out[group] = "missing"
        elif "stale" in states:
            out[group] = "stale"
        else:
            out[group] = "fresh"
    return out


def rollup_group_state(chunk_states: Iterable[str]) -> dict[str, Any]:
    """Roll per-chunk group states up to a chapter (or book) verdict.

    Any stale chunk makes the whole thing ``stale`` — one out-of-date verdict
    is enough to stop trusting the badge. Otherwise a gap is ``partial``, all
    gaps is ``not_run``, and everything current is ``done``.
    """
    counts = {"fresh": 0, "stale": 0, "missing": 0}
    for state in chunk_states:
        counts[state if state in counts else "missing"] += 1
    total = sum(counts.values())
    if total == 0 or counts["missing"] == total:
        state = "not_run"
    elif counts["stale"]:
        state = "stale"
    elif counts["missing"]:
        state = "partial"
    else:
        state = "done"
    return {"state": state, **counts}


def iter_chapter_chunks(project_dir: Path) -> dict[str, list[tuple[str, dict]]]:
    """Read ``chunks/*_chunk_*.json`` once into ``{chapter_id: [(chunk_id, data)]}``.

    Every caller of this has to hash each chunk's ``translated_text`` anyway, so
    it pays for the read regardless — doing it in one pass keeps the walk to a
    single sweep of the directory instead of one stat-and-open per lookup. Each
    payload carries an extra ``_mtime`` key for the legacy freshness fallback.
    """
    from collections import defaultdict

    out: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    chunks_dir = Path(project_dir) / "chunks"
    if not chunks_dir.exists():
        return out
    for path in sorted(chunks_dir.glob("*_chunk_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        chunk_id = data.get("id") or path.stem
        chapter_id = data.get("chapter_id") or chapter_id_from_chunk_id(chunk_id)
        data["_mtime"] = mtime
        out[chapter_id].append((chunk_id, data))
    return out


def chapter_chunk_states(
    project_dir: Path,
    chunks: list[tuple[str, dict]],
    *,
    groups: Optional[dict[str, tuple[str, ...]]] = None,
) -> list[dict[str, Any]]:
    """Per translated chunk: its group states, per-evaluator detail, and payload.

    The one place a chapter's evaluations are opened and hashed. Untranslated
    chunks are skipped — there is nothing to have judged — which is why the
    returned list can be shorter than ``chunks``.

    Returns one dict per translated chunk with ``chunk_id``, ``states``
    (``{group: fresh|stale|missing}``), ``detail`` (per-evaluator
    ``{state, basis}`` from :func:`evaluator_freshness_detail`) and
    ``evaluation`` (the persisted payload, or ``None``) so a caller wanting
    provenance does not re-read the file.
    """
    out: list[dict[str, Any]] = []
    for chunk_id, data in chunks:
        if not (data.get("translated_text") or "").strip():
            continue
        payload = load_chunk_evaluation(project_dir, chunk_id)
        detail = evaluator_freshness_detail(
            payload,
            chunk_text_sha(data.get("translated_text") or ""),
            chunk_mtime=data.get("_mtime"),
        )
        freshness = {name: str(entry["state"]) for name, entry in detail.items()}
        out.append({
            "chunk_id": chunk_id,
            "states": chunk_group_states(freshness, groups),
            "detail": detail,
            "evaluation": payload,
        })
    return out


def chapter_judge_status(
    project_dir: Path,
    chunks: list[tuple[str, dict]],
    *,
    groups: Optional[dict[str, tuple[str, ...]]] = None,
) -> tuple[dict[str, dict], list[dict[str, str]]]:
    """Roll a chapter's chunks up to one status per :data:`JUDGE_STATUS_GROUPS`.

    Returns ``(by_group, per_chunk_states)`` — the second value is handed back
    so the book-wide totals can be rolled up from the same per-chunk verdicts
    rather than from an average of chapter verdicts.
    """
    per_chunk = [rec["states"] for rec in chapter_chunk_states(project_dir, chunks, groups=groups)]
    by_group = {
        group: rollup_group_state(states.get(group, "missing") for states in per_chunk)
        for group in (groups or JUDGE_STATUS_GROUPS)
    }
    return by_group, per_chunk


def _load_project_glossary(project_dir: Path) -> Optional[Glossary]:
    """Best-effort glossary load from ``projects/<id>/glossary.json``."""
    glossary_path = Path(project_dir) / "glossary.json"
    if not glossary_path.exists():
        return None
    try:
        return load_glossary(glossary_path)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Failed to load glossary from %s: %s", glossary_path, e)
        return None


def load_project_ignored_terms(project_dir: Path) -> Optional[IgnoredTerms]:
    """Best-effort ignore-list load from ``projects/<id>/ignored_terms.json``.

    Public, unlike its glossary/blacklist siblings, because the routes that add
    and remove entries need it too. Returns ``None`` when absent or unreadable
    so a malformed file degrades to "nothing is ignored" rather than blanking
    the review queue.
    """
    path = Path(project_dir) / "ignored_terms.json"
    if not path.exists():
        return None
    try:
        return load_ignored_terms(path)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Failed to load ignored terms from %s: %s", path, e)
        return None


def _load_project_blacklist(project_dir: Path) -> Optional[Blacklist]:
    """Best-effort blacklist load.

    Resolution order:
    1. ``blacklist.json`` inside the project directory (per-project override).
    2. ``blacklist_path`` from ``app_config.json`` (system-wide default).
    """
    # 1. Per-project override
    project_bl = Path(project_dir) / "blacklist.json"
    if project_bl.exists():
        try:
            return load_blacklist(project_bl)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to load project blacklist from %s: %s", project_bl, e)
            return None

    # 2. System-wide default from app_config.json
    bl_path = get_blacklist_path()
    if bl_path and bl_path.exists():
        try:
            return load_blacklist(bl_path)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to load blacklist from %s: %s", bl_path, e)
    return None


def run_coded_evaluators(
    chunk: Chunk,
    *,
    glossary: Optional[Glossary] = None,
    blacklist: Optional[Blacklist] = None,
    enabled_evals: Optional[Iterable[str]] = None,
) -> tuple[list[EvalResult], dict[str, Any], list[NormalizedIssue]]:
    """Run the coded evaluators and return ``(results, aggregated, issues)``.

    ``issues`` is the flattened view-layer list ready for UI consumption.
    Failures inside individual evaluators are already swallowed by
    :func:`run_evaluator` and surfaced as ERROR issues in the returned
    :class:`EvalResult` list — the caller never needs to wrap this in its own
    try/except for evaluator crashes, only for programming errors in this
    pipeline.
    """
    if enabled_evals is not None:
        names = list(enabled_evals)
    else:
        system_filter = get_enabled_evaluators()
        if system_filter is not None:
            names = [n for n in system_filter if n in CODED_EVAL_NAMES]
        else:
            names = list(CODED_EVAL_NAMES)
    config = EvaluationConfig(enabled_evals=names)

    results = run_all_evaluators(chunk, config, glossary, blacklist)
    aggregated = aggregate_results(results)

    normalized: list[NormalizedIssue] = []
    for result in results:
        normalized.extend(fan_out_issues(result, chunk))

    return results, aggregated, normalized


def evaluate_and_persist_chunk(
    project_dir: Path,
    chunk: Chunk,
    *,
    glossary: Optional[Glossary] = None,
    blacklist: Optional[Blacklist] = None,
    preserve_llm_judge: bool = True,
    enabled_evals: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Run coded evaluators on ``chunk`` and persist results.

    Args:
        project_dir: ``projects/<id>/`` directory.
        chunk: Chunk with a fresh ``translated_text`` to evaluate.
        glossary: Optional preloaded glossary. Loaded from disk if omitted.
        blacklist: Optional preloaded blacklist. Loaded from disk if omitted
            (project-level then app_config fallback).
        preserve_llm_judge: If ``True``, keep any existing ``llm_judge`` block
            from a previous run so rerunning the coded evaluators doesn't wipe
            out the LLM judge output.
        enabled_evals: Optional subset of :data:`CODED_EVAL_NAMES` to run. The
            Review tab's "rerun deterministic" passes this so a user can redo
            just one evaluator; ``None`` runs the configured set.

    Returns:
        Dict with keys suitable for JSON-ing back to the frontend:
        ``aggregated``, ``issues`` (list of ``NormalizedIssue.to_dict()``),
        and ``enabled_evals``.
    """
    project_dir = Path(project_dir)
    if glossary is None:
        glossary = _load_project_glossary(project_dir)
    if blacklist is None:
        blacklist = _load_project_blacklist(project_dir)

    results, aggregated, normalized = run_coded_evaluators(
        chunk, glossary=glossary, blacklist=blacklist, enabled_evals=enabled_evals,
    )
    actually_ran = [r.eval_name for r in results]

    previous = load_chunk_evaluation(project_dir, chunk.id)

    existing_llm = None
    existing_judges = None
    stale_mark = None
    if preserve_llm_judge and previous is not None:
        existing_llm = previous.get("llm_judge")
        existing_judges = previous.get("judges")
        if previous.get("stale") and existing_judges:
            stale_mark = {
                "stale_since": previous.get("stale_since"),
                "stale_reason": previous.get("stale_reason"),
            }

    # A narrowed rerun ("just re-run grammar") must not delete the other
    # evaluators' findings: save_chunk_evaluation overwrites the file wholesale,
    # so carry the untouched ones forward explicitly. Their ledger entries keep
    # their original hash — carrying a finding forward is not a claim that the
    # evaluator has seen the current text.
    carried_issues: list[dict[str, Any]] = []
    persisted_results: list[EvalResult] = list(results)
    if enabled_evals is not None and previous is not None:
        ran = set(actually_ran)
        carried_names: set[str] = set()
        for raw in previous.get("results") or []:
            if not isinstance(raw, dict) or raw.get("eval_name") in ran:
                continue
            rehydrated = _rehydrate_result(raw)
            if rehydrated is not None:
                persisted_results.append(rehydrated)
                carried_names.add(rehydrated.eval_name)
        carried_issues = [
            i for i in (previous.get("normalized_issues") or [])
            if isinstance(i, dict) and i.get("eval_name") in carried_names
        ]
        if carried_names:
            aggregated = aggregate_results(persisted_results)

    persisted_evals = [r.eval_name for r in persisted_results]

    save_chunk_evaluation(
        project_dir,
        chunk.id,
        persisted_results,
        aggregated,
        list(normalized) + carried_issues,
        enabled_evals=persisted_evals,
        llm_judge=existing_llm,
        judges=existing_judges,
        stale_mark=stale_mark,
        stamp_evals=actually_ran,
        # Hash the text these results were produced from, not whatever is on
        # disk by the time we write: a background run evaluates at T0 and the
        # chunk editor can save at T1, which would stamp the ledger with a hash
        # the findings never saw and leave the badge permanently "fresh".
        text_sha=chunk_text_sha(chunk.translated_text),
    )

    result: dict = {
        "aggregated": aggregated,
        "issues": [issue.to_dict() for issue in normalized] + carried_issues,
        "enabled_evals": persisted_evals,
    }
    if stale_mark:
        result["stale"] = True
        result["stale_since"] = stale_mark.get("stale_since")
        result["stale_reason"] = stale_mark.get("stale_reason")
    return result


__all__ = [
    "CODED_EVAL_NAMES",
    "JUDGE_STATUS_GROUPS",
    "save_chunk_evaluation",
    "load_chunk_evaluation",
    "merge_llm_judge_result",
    "merge_judge_result",
    "mark_evaluation_stale",
    "append_feedback",
    "issue_key",
    "build_dismissed",
    "is_dismissed",
    "is_ignored",
    "issue_term",
    "count_ignored_hits",
    "IgnoreHits",
    "load_project_ignored_terms",
    "load_feedback_for_chunk",
    "load_project_summary",
    "run_coded_evaluators",
    "evaluate_and_persist_chunk",
    "chunk_text_sha",
    "current_chunk_sha",
    "evaluator_freshness",
    "chunk_group_states",
    "rollup_group_state",
]
