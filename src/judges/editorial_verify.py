"""Adjudication pass over the editorial judge's candidate findings.

The editorial judge reads Spanish alone and proposes candidates. This pass gives
every surviving candidate a second opinion — CONFIRM, RETRACT or RECLASSIFY —
and attaches the English original to the ones that asked for it.

**Why it adjudicates everything, not only the source-dependent findings.** The
obvious design is to fire a bilingual call only when a candidate set
``source_check`` to ``recommended``/``required``. But the Spanish-only findings
are the majority, and they are where the false positives live: the measured
false-positive rate of the existing judges is 34-45%, and none of that is
fidelity error. Adjudicating the whole set costs the same one call per chunk —
the English is attached per candidate, not per call — and it makes "how often did
the second pass change the outcome" answerable for every finding rather than for
a self-selected slice.

The three seams (:func:`build_prompt`, :func:`parse_verdicts`,
:func:`apply_verdicts`) are backend-agnostic, exactly like the judge base class:
the API path calls the LLM between them, the headless path writes the prompt to a
file and reads a worker's draft, and the persisted result is identical either way.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.judges import llm_io
from src.judges.editorial_judge import normalize_category, normalize_source_check
from src.judges.neighborhood import english_window, load_alignment_rows
from src.judges.scoring import compliance_score, finding_to_issue

logger = logging.getLogger(__name__)

VERIFY_TEMPLATE = "judge_editorial_verify.txt"

VERDICTS = ("CONFIRM", "RETRACT", "RECLASSIFY")

#: Characters of Spanish either side of the excerpt when the alignment cannot
#: place it. Enough to judge a sentence in its paragraph.
_FALLBACK_CONTEXT_CHARS = 400


def _chapter_id(chunk_id: str) -> str:
    marker = chunk_id.rfind("_chunk_")
    return chunk_id[:marker] if marker > 0 else chunk_id


def _spanish_fallback(translated_text: str, excerpt: str) -> str:
    """Spanish around ``excerpt`` taken straight from the chunk.

    Used when the alignment cannot place the excerpt. The candidate still gets
    adjudicated — on the Spanish, which is what the first reader had — rather
    than being dropped for want of a window.
    """
    index = translated_text.find(excerpt)
    if index < 0:
        return ""
    start = max(0, index - _FALLBACK_CONTEXT_CHARS)
    end = min(len(translated_text), index + len(excerpt) + _FALLBACK_CONTEXT_CHARS)
    return translated_text[start:end].strip()


def collect_candidates(
    result: dict[str, Any], chunk_id: str, translated_text: str
) -> list[dict[str, Any]]:
    """Pull the pre-adjudication candidates off a persisted editorial result.

    Falls back to reconstructing them from ``issues`` for a result written
    before ``metadata.candidates`` existed, so an early wave is still
    verifiable. ``key`` is the finding's :attr:`Issue.finding_key` — the same
    identity a dismissal is recorded against, so a verdict and a human mark
    always refer to the same finding.
    """
    metadata = result.get("metadata") or {}
    candidates = [c for c in (metadata.get("candidates") or []) if isinstance(c, dict)]

    if not candidates:
        for issue in result.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            candidates.append(
                {
                    "rule": (issue.get("rule_id") or "other"),
                    "category": issue.get("category") or "NATURALNESS",
                    "severity": issue.get("severity") or "warning",
                    "confidence": "high",
                    "excerpt": issue.get("location") or "",
                    "message": issue.get("message") or "",
                    "suggestion": issue.get("suggestion") or "",
                    "source_check": "recommended",
                }
            )

    enriched: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        item.setdefault("chunk_id", chunk_id)
        excerpt = str(item.get("excerpt") or "")
        item["key"] = finding_to_issue(
            item, default_message="Editorial defect.", stable_identity=True
        ).finding_key
        item.setdefault("_translated_text", translated_text)
        item["_excerpt"] = excerpt
        enriched.append(item)
    return enriched


def attach_context(
    project_dir: Path, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach Spanish context, and English where the candidate asked for it.

    Alignment rows are loaded once per (chapter, chunk) rather than per
    candidate — a chunk with four findings would otherwise re-read and re-fold
    the same chapter four times.
    """
    rows_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    out: list[dict[str, Any]] = []

    for candidate in candidates:
        item = dict(candidate)
        chunk_id = str(item.get("chunk_id") or "")
        chapter_id = _chapter_id(chunk_id)
        excerpt = str(item.get("_excerpt") or item.get("excerpt") or "")
        translated_text = str(item.get("_translated_text") or "")

        cache_key = (chapter_id, chunk_id)
        if cache_key not in rows_cache:
            rows_cache[cache_key] = load_alignment_rows(project_dir, chapter_id, chunk_id)

        window = english_window(
            project_dir, chapter_id, excerpt, chunk_id=chunk_id, rows=rows_cache[cache_key]
        )
        wants_source = normalize_source_check(item.get("source_check")) != "not_needed"

        item["_spanish_context"] = (
            window.spanish_text() if window.matched else _spanish_fallback(translated_text, excerpt)
        )
        item["_english_context"] = (
            window.english_text() if (wants_source and window.matched) else ""
        )
        item["_source_available"] = bool(item["_english_context"])
        item["_source_requested"] = wants_source
        out.append(item)
    return out


def _candidate_block(candidate: dict[str, Any]) -> str:
    parts = [f'<candidate key="{candidate.get("key")}">']
    parts.append(f'<rule>{candidate.get("rule") or "other"}</rule>')
    parts.append(f'<category>{normalize_category(candidate.get("category"))}</category>')
    parts.append(f'<severity>{candidate.get("severity") or "warning"}</severity>')
    parts.append(f'<problem>{str(candidate.get("message") or "").strip()}</problem>')
    parts.append(
        f'<proposed_fix>{str(candidate.get("suggestion") or "").strip()}</proposed_fix>'
    )
    parts.append(f'<excerpt>\n{candidate.get("_excerpt") or ""}\n</excerpt>')
    spanish = str(candidate.get("_spanish_context") or "").strip()
    if spanish:
        parts.append(f"<spanish_context>\n{spanish}\n</spanish_context>")
    english = str(candidate.get("_english_context") or "").strip()
    if english:
        parts.append(f"<english_context>\n{english}\n</english_context>")
    elif candidate.get("_source_requested"):
        parts.append(
            "<english_context_unavailable>The first reader asked for the original "
            "here, but the excerpt could not be located in the chapter alignment. "
            "Judge on the Spanish alone and do not assume the English "
            "differs.</english_context_unavailable>"
        )
    parts.append("</candidate>")
    return "\n".join(parts)


def build_prompt(candidates: list[dict[str, Any]], context: dict[str, Any]) -> str:
    """Render the adjudication prompt for one batch of candidates."""
    template = llm_io.load_template(VERIFY_TEMPLATE)
    return llm_io.render(
        template,
        {
            "style_guide": str(context.get("style_guide") or "").strip()
            or "(no style guide for this book.)",
            "glossary": str(context.get("glossary") or "").strip()
            or "(no glossary for this book.)",
            "candidates": "\n\n".join(_candidate_block(c) for c in candidates),
        },
    )


def build_prompt_parts(
    candidates: list[dict[str, Any]], context: dict[str, Any]
) -> tuple[str, str]:
    """Split the prompt into a cacheable prefix and a per-batch suffix.

    ``prefix + suffix`` is byte-identical to :func:`build_prompt`, matching the
    contract in :meth:`src.judges.base.Judge.build_prompt_parts`.
    """
    from src.judges.base import _CACHE_PREFIX_SPLIT_MARKER

    prompt = build_prompt(candidates, context)
    index = prompt.find(_CACHE_PREFIX_SPLIT_MARKER)
    if index < 0:
        return "", prompt
    return prompt[:index], prompt[index:]


def parse_verdicts(raw: str) -> dict[str, dict[str, Any]]:
    """Parse an adjudication response into ``{key: verdict_dict}``.

    Raises :class:`~src.judges.llm_io.JudgeParseError` on unparseable output, so
    the API path can retry and the draft path can mark the batch for re-spawn.
    """
    data = llm_io.parse_judge_json(raw, ("verdicts",))
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, dict):
        raise llm_io.JudgeParseError("`verdicts` must be an object keyed by candidate key")

    out: dict[str, dict[str, Any]] = {}
    for key, value in verdicts.items():
        if not isinstance(value, dict):
            continue
        decision = str(value.get("verdict") or "").strip().upper()
        if decision not in VERDICTS:
            # An unrecognized verdict must not silently delete a finding, and
            # must not silently keep one either. CONFIRM is the conservative
            # reading: the finding stands as the first reader wrote it, and the
            # unparsed value is preserved on the record for the metrics report.
            logger.warning("Unknown verdict %r on %s; treating as CONFIRM", decision, key)
            decision = "CONFIRM"
        out[str(key)] = {**value, "verdict": decision}
    return out


def apply_verdicts(
    result: dict[str, Any],
    candidates: list[dict[str, Any]],
    verdicts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Fold adjudication verdicts into a persisted editorial ``EvalResult`` dict.

    Survivors become ``issues``; retracted findings move to
    ``metadata.retracted`` with their reason and reclassified ones to
    ``metadata.reclassified_findings`` with both severities.
    ``metadata.candidates`` is left
    exactly as the first pass wrote it — the pre-adjudication set is the only
    record of what was proposed, and the retraction rate is measured against it.

    A candidate with no verdict is kept. A model that omits a key has not
    retracted the finding, and silently dropping it would make the pass look
    more decisive than it was.
    """
    survivors: list[dict[str, Any]] = []
    retracted: list[dict[str, Any]] = []
    reclassified_findings: list[dict[str, Any]] = []
    source_used = 0

    for candidate in candidates:
        key = str(candidate.get("key") or "")
        verdict = verdicts.get(key)
        if verdict is None:
            logger.warning("No verdict returned for candidate %s; keeping it", key)
            survivors.append(_clean(candidate))
            continue

        decision = verdict.get("verdict")
        if verdict.get("used_source") and candidate.get("_source_available"):
            source_used += 1

        if decision == "RETRACT":
            retracted.append(
                {
                    "finding_key": key,
                    "rule": candidate.get("rule"),
                    "category": normalize_category(candidate.get("category")),
                    "excerpt": candidate.get("_excerpt") or candidate.get("excerpt"),
                    "reason": str(verdict.get("reason") or "").strip(),
                    "used_source": bool(verdict.get("used_source")),
                }
            )
            continue

        updated = _clean(candidate)
        if decision == "RECLASSIFY":
            # Record the move before applying it: ``updated`` is overwritten in
            # place below, and the survivor that reaches ``issues`` keeps only the
            # new severity. Without this a reclassification is a bare count, and
            # "read old -> new before treating the suggestion as approved" is an
            # instruction nothing on disk can answer.
            old_category = normalize_category(candidate.get("category"))
            old_severity = str(candidate.get("severity") or "").strip().lower()
            if verdict.get("category"):
                updated["category"] = normalize_category(verdict.get("category"))
            if verdict.get("severity"):
                updated["severity"] = str(verdict["severity"]).strip().lower()
            if str(verdict.get("message") or "").strip():
                updated["message"] = str(verdict["message"]).strip()
            reclassified_findings.append(
                {
                    "finding_key": key,
                    "rule": candidate.get("rule"),
                    "excerpt": candidate.get("_excerpt") or candidate.get("excerpt"),
                    "category": old_category,
                    "new_category": updated.get("category"),
                    "severity": old_severity,
                    "new_severity": updated.get("severity"),
                    "reason": str(verdict.get("reason") or "").strip(),
                    "used_source": bool(verdict.get("used_source")),
                }
            )
        if str(verdict.get("suggestion") or "").strip():
            updated["suggestion"] = str(verdict["suggestion"]).strip()
        updated["verdict"] = decision
        updated["verdict_reason"] = str(verdict.get("reason") or "").strip()
        survivors.append(updated)

    issues = [
        finding_to_issue(f, default_message="Editorial defect.", stable_identity=True)
        for f in survivors
    ]

    patched = dict(result)
    patched["issues"] = [i.model_dump(mode="json") for i in issues]
    patched["score"] = compliance_score(survivors)
    patched["passed"] = not any(i.severity.value == "error" for i in issues)

    metadata = dict(patched.get("metadata") or {})
    requested = sum(1 for c in candidates if c.get("_source_requested"))
    metadata.update(
        {
            "verified": True,
            "verified_at": datetime.now().isoformat(),
            "verify_prompt_version": llm_io.prompt_version(VERIFY_TEMPLATE),
            "candidates_adjudicated": len(candidates),
            "confirmed": len(survivors) - len(reclassified_findings),
            # ``reclassified`` stays an int and the detail arrives beside it under
            # a new key, rather than mirroring ``retracted``/``retracted_count``:
            # six consumers already read this one as a number (editorial_metrics,
            # the wave rollup, /review-status, the dashboard tooltip), and the
            # asymmetry is cheaper than a coordinated rename.
            "reclassified": len(reclassified_findings),
            "reclassified_findings": reclassified_findings,
            "retracted": retracted,
            "retracted_count": len(retracted),
            "source_requested": requested,
            "source_attached": sum(1 for c in candidates if c.get("_source_available")),
            "source_used": source_used,
            "finding_count": len(issues),
            "clean": not issues,
        }
    )
    patched["metadata"] = metadata
    patched["error_count"] = sum(1 for i in issues if i.severity.value == "error")
    patched["warning_count"] = sum(1 for i in issues if i.severity.value == "warning")
    patched["info_count"] = sum(1 for i in issues if i.severity.value == "info")
    return patched


def _clean(candidate: dict[str, Any]) -> dict[str, Any]:
    """Drop the underscore-prefixed working fields before persisting."""
    return {k: v for k, v in candidate.items() if not k.startswith("_")}


def verdict_detail(chunk_id: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """The findings this chunk's adjudication actually moved, tagged with the chunk.

    Everything a wave-level report needs to say *which* ones changed, without
    walking one ``evaluations/<chunk>.json`` per chunk to find out. Confirmations
    are omitted on purpose: they are the majority and the outcome that changed
    nothing, so a list of them is the flood, not the signal.
    """
    detail: list[dict[str, Any]] = []
    for record in metadata.get("retracted") or []:
        detail.append({"chunk_id": chunk_id, "verdict": "RETRACT", **record})
    for record in metadata.get("reclassified_findings") or []:
        detail.append({"chunk_id": chunk_id, "verdict": "RECLASSIFY", **record})
    return detail


def verify_result(
    project_dir: Path,
    chunk_id: str,
    result: dict[str, Any],
    translated_text: str,
    context: dict[str, Any],
    *,
    candidates: Optional[list[dict[str, Any]]] = None,
    call: Optional[Any] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """API path: adjudicate one chunk's candidates and return the patched result.

    Returns ``(patched_result, info)``. ``info`` reports what happened for the
    CLI's JSON output. ``call`` is a test seam with the signature of
    :func:`src.judges.llm_io.call_judge`. Pass ``candidates`` when the caller has
    already run :func:`attach_context` — collecting them twice re-reads and
    re-folds the chapter alignment for no gain.

    An unparseable adjudication leaves the pass-1 result untouched rather than
    guessing: a first-pass verdict nobody has second-guessed is a known,
    honestly-labelled state (``verified: False``), and silently confirming
    everything would misreport it as adjudicated.
    """
    caller = call or llm_io.call_judge
    if candidates is None:
        candidates = attach_context(
            project_dir, collect_candidates(result, chunk_id, translated_text)
        )
    if not candidates:
        return result, {"chunk_id": chunk_id, "status": "no_candidates", "adjudicated": 0}

    prefix, suffix = build_prompt_parts(candidates, context)
    raw = caller(
        prefix + suffix,
        provider=context.get("judge_provider"),
        model=context.get("judge_model"),
        call_type="judge_editorial_verify",
        cache_prefix=prefix or None,
    )
    try:
        verdicts = parse_verdicts(raw)
    except llm_io.JudgeParseError as exc:
        logger.error("Editorial verification unparseable on %s: %s", chunk_id, exc)
        return result, {"chunk_id": chunk_id, "status": "parse_error", "error": str(exc)}

    patched = apply_verdicts(result, candidates, verdicts)
    meta = patched.get("metadata") or {}
    return patched, {
        "chunk_id": chunk_id,
        "status": "ok",
        "adjudicated": len(candidates),
        "confirmed": meta.get("confirmed"),
        "reclassified": meta.get("reclassified"),
        "retracted": meta.get("retracted_count"),
        "source_attached": meta.get("source_attached"),
        "source_used": meta.get("source_used"),
        "verdict_detail": verdict_detail(chunk_id, meta),
    }


__all__ = [
    "VERDICTS",
    "VERIFY_TEMPLATE",
    "apply_verdicts",
    "attach_context",
    "build_prompt",
    "build_prompt_parts",
    "collect_candidates",
    "parse_verdicts",
    "verdict_detail",
    "verify_result",
]
