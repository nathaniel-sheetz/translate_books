"""The synthesis wave: a model reads the evidence and proposes upstream fixes.

``prepare`` -> ``fanout`` -> ``commit``, the same shape as every headless wave:

- :func:`prepare` runs the detectors, builds the budgeted evidence
  (:mod:`src.book_review.evidence`), and renders one job per theme plus a
  shared preamble (instructions, style guide, address map) under
  ``.harness/book_review/synthesis/``. No spend.
- :func:`fanout` runs the jobs as one headless wave
  (:func:`src.decisions.wave.run_wave`, rooted at this stage's directory). A
  job whose draft parses is skipped; a draft ``commit`` cannot parse is run
  again, so a re-run resumes.
- :func:`commit` parses the drafts, validates every patch against the book as
  it is now (:mod:`src.book_review.schema`), stamps the state of every
  glossary entry and address pair a patch edits by name
  (:func:`src.book_review.patches.stamp`), and writes ``recommendations.json``
  plus the dated report. No spend, and nothing in the book changes.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from src.book_review import evidence as ev
from src.book_review import progress, review_dir
from src.book_review.corpus import load_book
from src.book_review.report import write_recommendations, write_signals
from src.book_review.patches import stamp
from src.book_review.schema import check_recommendation, reconcile
from src.book_review.signals import run_all

COMMAND = "book_review"
TEMPLATE = "book_review_synthesize.txt"


def synthesis_dir(project_dir: Path) -> Path:
    return review_dir(project_dir) / "synthesis"


def _manifest_path(project_dir: Path) -> Path:
    return synthesis_dir(project_dir) / "manifest.json"


def recommendations_path(project_dir: Path) -> Path:
    return review_dir(project_dir) / "recommendations.json"


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message}


def render_body(theme: str, job: dict[str, Any], background: list[dict[str, Any]]) -> str:
    doc = {"theme": theme, "job_id": job["job_id"], "items": job["items"],
           "background_counts": background}
    return "EVIDENCE (JSON):\n" + json.dumps(doc, ensure_ascii=False, indent=1) + "\n"


def prepare(
    project_dir: Path, *, cli: Optional[str] = None, worker_model: Optional[str] = None,
    effort: Optional[str] = None, keep_drafts: bool = False,
) -> dict[str, Any]:
    from src.harness.profile import resolve_profile, usage_log_for
    from src.judges.llm_io import load_template, prompt_version, render

    project_dir = Path(project_dir)
    corpus = load_book(project_dir)
    if not corpus.findings:
        return _error("this book has no judge findings yet; run `judge` first")
    signals_result = run_all(corpus)
    write_signals(corpus, signals_result)
    evidence = ev.build(corpus, signals_result)
    if not evidence["themes"]:
        # A previous run's manifest would let `commit` re-stamp its old drafts as new.
        _manifest_path(project_dir).unlink(missing_ok=True)
        return {"status": "ok", "jobs": 0, "evidence": evidence,
                "instructions": "The detectors found nothing to synthesize."}

    out_dir = synthesis_dir(project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (review_dir(project_dir) / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    preamble = render(load_template(TEMPLATE), {"book_context": ev.preamble_context(corpus)})
    preamble_path = out_dir / "preamble.txt"
    preamble_path.write_text(preamble, encoding="utf-8")

    jobs = []
    for theme in evidence["themes"]:
        for job in theme["jobs"]:
            body_path = out_dir / f"{job['job_id']}.body.txt"
            draft_path = out_dir / f"{job['job_id']}.draft.json"
            body = render_body(theme["theme"], job, evidence["background"])
            body_path.write_text(body, encoding="utf-8")
            if not keep_drafts:
                draft_path.unlink(missing_ok=True)
            jobs.append({
                "id": job["job_id"], "theme": theme["theme"],
                "signals": [i["signal_id"] for i in job["items"]],
                "body_path": str(body_path), "draft_path": str(draft_path),
                "estimated_tokens": ev.estimate_tokens(preamble + body),
            })

    prof = resolve_profile(project_dir, command=COMMAND, cli=cli, worker_model=worker_model,
                           effort=effort, usage_log=usage_log_for(project_dir, COMMAND))
    manifest = {
        "project": project_dir.name,
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "prompt_version": prompt_version(TEMPLATE),
        "inputs_hash": evidence["inputs_hash"],
        "preamble_path": str(preamble_path),
        "worker_model": prof.worker_model,
        "cli": prof.cli,
        "effort": prof.effort,
        "jobs": jobs,
    }
    _manifest_path(project_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "status": "ok",
        "jobs": len(jobs),
        "by_theme": {t["theme"]: len(t["jobs"]) for t in evidence["themes"]},
        "input_tokens": sum(j["estimated_tokens"] for j in jobs),
        "omitted": evidence["omitted"],
        "effective": prof.to_payload(),
        "manifest_path": str(_manifest_path(project_dir)),
    }


def _parsed_draft(path: Path) -> bool:
    """True when ``commit`` can parse this draft. A non-empty non-JSON file is not done."""
    from src.judges.llm_io import JudgeParseError, parse_judge_json

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if not text.strip():
        return False
    try:
        parse_judge_json(text, ["recommendations"])
    except JudgeParseError:
        return False
    return True


def fanout(project_dir: Path, **kwargs: Any) -> dict[str, Any]:
    from src.decisions.wave import fanout_error, load_manifest, run_wave

    project_dir = Path(project_dir)
    manifest, err = load_manifest(
        _manifest_path(project_dir), missing="no synthesis manifest; run `prepare` first",
    )
    if err:
        return fanout_error(err)
    return run_wave(project_dir, manifest, command=COMMAND,
                    root=synthesis_dir(project_dir), draft_done=_parsed_draft, **kwargs)


def commit(project_dir: Path, *, today: Optional[str] = None) -> dict[str, Any]:
    from src.decisions.wave import load_manifest
    from src.judges.llm_io import JudgeParseError, parse_judge_json

    project_dir = Path(project_dir)
    manifest, err = load_manifest(
        _manifest_path(project_dir), missing="no synthesis manifest; run `prepare` first",
    )
    if err:
        return _error(err)
    corpus = load_book(project_dir)
    evidence_doc = json.loads((review_dir(project_dir) / "evidence.json").read_text(encoding="utf-8"))
    known = {i["signal_id"] for t in evidence_doc["themes"] for j in t["jobs"] for i in j["items"]}
    known |= {f.id for f in corpus.findings}
    artifacts = ev.load_artifacts(project_dir)

    recommendations: list[dict[str, Any]] = []
    no_action: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    missing: list[str] = []
    for job in manifest["jobs"]:
        draft = Path(job["draft_path"])
        if not draft.exists() or not draft.read_text(encoding="utf-8").strip():
            missing.append(job["id"])
            continue
        try:
            data = parse_judge_json(draft.read_text(encoding="utf-8"), ["recommendations"])
        except JudgeParseError as exc:
            failed.append({"id": job["id"], "error": str(exc)[:300]})
            continue
        for raw in data.get("recommendations") or []:
            rec, problems = check_recommendation(raw, job["theme"], corpus, known)
            if rec is None:
                rejected.append({"job": job["id"], "title": (raw or {}).get("title") if isinstance(raw, dict) else None,
                                 "problems": problems})
                continue
            try:
                rec["targets"] = stamp(artifacts, rec["patches"])
            except ValueError as exc:
                rejected.append({"job": job["id"], "title": rec["title"], "problems": [str(exc)]})
                continue
            recommendations.append(rec)
        for item in data.get("no_action") or []:
            if isinstance(item, dict) and item.get("signal_id"):
                no_action.append({"signal_id": str(item["signal_id"]),
                                  "reason": str(item.get("reason") or "")})

    # One id per title within a book. A later proposal with the same id is
    # rejected rather than dropped, so the operator can see it.
    seen: dict[str, dict[str, Any]] = {}
    for rec in recommendations:
        if rec["id"] in seen:
            rejected.append({
                "title": rec["title"],
                "problems": [f"same id as an earlier recommendation ({rec['id']}); kept the first"],
            })
            continue
        seen[rec["id"]] = rec
    recommendations = sorted(seen.values(), key=lambda r: (r["priority"], r["theme"], r["title"]))
    reconcile(recommendations)

    doc = {
        "project": project_dir.name,
        "committed_at": datetime.now().isoformat(timespec="seconds"),
        "prompt_version": manifest.get("prompt_version"),
        "inputs_hash": manifest.get("inputs_hash"),
        "worker_model": manifest.get("worker_model"),
        "cli": manifest.get("cli"),
        "recommendations": recommendations,
        "no_action": no_action,
        "rejected": rejected,
        "omitted": evidence_doc.get("omitted") or [],
        "coverage": evidence_doc.get("coverage"),
    }
    recommendations_path(project_dir).write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path = write_recommendations(corpus, doc, today=today or date.today().isoformat())
    progress.record_review(project_dir, kind="synthesis", report_path=report_path)
    return {
        "status": "ok" if not failed else "partial",
        "recommendations": len(recommendations),
        "by_priority": {p: sum(1 for r in recommendations if r["priority"] == p) for p in (1, 2, 3)},
        "no_action": len(no_action),
        "rejected": len(rejected),
        "failed": failed,
        "missing": missing,
        "recommendations_path": str(recommendations_path(project_dir)),
        "report_path": report_path,
    }
