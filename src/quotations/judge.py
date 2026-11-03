"""
The judge: one verdict and one confidence for every card that would change the text.

A filter over the mapping wave, never an editor. It reads each proposed edit in its
sentences, against the verse, and answers ``apply``, ``partial``, ``reject`` or
``toss_up``. It writes no chunk and changes neither ``mapped.json`` nor the board's
choices or fingerprint; the user still decides on the board.

The prompt turns on whether the words sit inside quotation marks. Inside « » there
is no misquote, so the NBLA's words stand and any other fix works around them
(memory ``feedback_quotation_no_misquote_in_marks``). Outside them the narrator's
person, number and tense are kept.

**Coded facts** go to the model with each edit (:func:`edit_facts`): where the words
sit, what kind of change it is, and three defects code can see for itself. A card
with such a defect cannot come back ``apply``: :func:`clamp` makes it ``partial`` and
keeps what the model said.

**The wave** follows :mod:`src.decisions.wave`: :func:`write_jobs` renders jobs and a
manifest into a directory, :func:`run_jobs` runs them, :func:`collect` reads the
drafts back. A live pass on a book uses ``.harness/quotations/judge/``; the replay in
``scripts/quotation_judge_gold.py`` uses the same three with its own frozen items, so
the gold set is judged from exactly the view a live card gets.

**Verdicts** live in ``judge.json``, keyed by card with a hash of its edits, so a
revised card's verdict goes stale instead of describing a diff that is gone.

Only the quote edits are judged. Echo edits are sub-items the user ticks by hand, and
no mark says anything about them.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.decisions.wave import fanout_error, load_manifest, run_wave
from src.judges.llm_io import JudgeParseError, extract_json, load_template, prompt_version, render
from src.quotations import quotations_dir
from src.quotations import map as qmap
from src.quotations import scan as qscan
from src.quotations.index import plain

TEMPLATE = "quotation_judge.txt"
EXAMPLES = "quotation_judge_examples.json"
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_CACHE_SPLIT = "# ---8<--- cache split ---8<---"

# Wave type, for ``state.COMMAND_EFFORT_DEFAULTS`` / ``headless_effort_quotations_judge``.
COMMAND = "quotations_judge"

VERDICT_APPLY = "apply"
VERDICT_PARTIAL = "partial"
VERDICT_REJECT = "reject"
VERDICT_TOSS_UP = "toss_up"
VERDICTS = (VERDICT_APPLY, VERDICT_PARTIAL, VERDICT_REJECT, VERDICT_TOSS_UP)

#: How a partial's fix works: around the quote, trimming it, moving words out of the
#: marks, removing the marks, repairing a mechanical slip, or (outside the marks)
#: taking only some of the proposal's words.
FIX_KINDS = ("around", "trim", "move_out", "remove_marks", "repair", "blend")

BLOCK_UNBALANCED = "unbalanced_marks"
BLOCK_NESTED = "nested_double_marks"
BLOCK_CAPITAL = "divine_capital_outside_marks"
_BLOCK_FIX = {
    BLOCK_UNBALANCED: "Repair the paired marks the proposal leaves unbalanced.",
    BLOCK_NESTED: "Use ‹ › for the quotation inside « ».",
    BLOCK_CAPITAL: "Outside quotation marks, don't capitalize the pronouns, and write Señor, not SEÑOR.",
}

DEFAULT_ITEMS_PER_JOB = 15
#: Rows before the edit read for an open « ». A quotation rarely runs longer, and a
#: stray mark earlier in the chapter must not make everything after it read as quoted.
_HEAD_ROWS = 4

_PAIRS = (("«", "»"), ("‹", "›"), ("¿", "?"), ("¡", "!"), ("[", "]"), ("(", ")"))
_DIVINE = re.compile(r"\b(?:Él|Tú|Ti|Tu|Tus|Su|Sus|Suyo|Suya|Suyos|Suyas|Mi|Mis|Mío|Mía|Aquel|SEÑOR)\b")
_NOT_WORD = re.compile(r"[\W_]+", re.UNICODE)


def judge_dir(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "judge"


def verdicts_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "judge.json"


# ── coded facts ──────────────────────────────────────────────────────────────

def quote_depth(head: str) -> int:
    """How many « are still open at the end of ``head``; a stray » never goes below 0."""
    depth = 0
    for ch in head:
        if ch == "«":
            depth += 1
        elif ch == "»":
            depth = max(0, depth - 1)
    return depth


def change_kind(old: str, new: str) -> str:
    """``case``, ``punctuation``, ``case_and_punctuation`` or ``words``."""
    if old.casefold() == new.casefold():
        return "case"
    if _NOT_WORD.sub("", old) == _NOT_WORD.sub("", new):
        return "punctuation"
    if _NOT_WORD.sub("", old).casefold() == _NOT_WORD.sub("", new).casefold():
        return "case_and_punctuation"
    return "words"


def _imbalance(text: str, opener: str, closer: str) -> int:
    return text.count(opener) - text.count(closer)


def edit_facts(old: str, new: str, head: str, kind: Optional[str]) -> dict[str, Any]:
    """What code can see about one edit. ``head`` is the text before ``old``.

    ``place`` is ``inside`` when the words sit in « » (or the span itself carries
    them), ``epigraph`` for an epigraph, whose words are set off the same way, and
    ``outside`` otherwise. ``blocks`` are the defects that rule out ``apply``.
    """
    open_marks = quote_depth(head)
    place = "inside" if open_marks or "«" in old or "»" in old else "outside"
    if place == "outside" and kind == "epigraph":
        place = "epigraph"
    blocks = []
    # Only a worsening counts: a span can start or end mid-pair, and an edit that
    # repairs a stray mark is doing its job.
    if any(abs(_imbalance(new, a, b)) > abs(_imbalance(old, a, b)) for a, b in _PAIRS):
        blocks.append(BLOCK_UNBALANCED)
    if open_marks and "«" in new:
        blocks.append(BLOCK_NESTED)
    if place == "outside":
        # A word opening the span is capitalized for the sentence, not for God.
        added = {m.group(0) for m in _DIVINE.finditer(new) if m.start() > 0}
        if added - set(_DIVINE.findall(old)):
            blocks.append(BLOCK_CAPITAL)
    return {"place": place, "change": change_kind(old, new), "blocks": blocks}


# ── the view of one card ─────────────────────────────────────────────────────

def edits_hash(edits: list[dict[str, Any]]) -> str:
    pairs = [[e["old"], e["new"]] for e in edits if e["old"] != e["new"]]
    return hashlib.sha256(json.dumps(pairs, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _edit_view(rows: dict[int, str], edit: dict[str, Any], kind: Optional[str]) -> dict[str, Any]:
    """One edit in its sentences: the rows around it, before and after."""
    old, new = edit["old"], edit["new"]
    # Alignment can leave the words a row or two from where the edit names them.
    idx = next((i for i in (edit["es_idx"], edit["es_idx"] + 1, edit["es_idx"] - 1, edit["es_idx"] + 2, edit["es_idx"] - 2)
                if old in (rows.get(i) or "")), None)
    if idx is None:
        # Never pass the bare words off as a sentence: the neighbours would read as
        # if the book repeated itself, and the model would judge that instead.
        return {"old": old, "new": new, "before": "(not available)", "after": "(not available)",
                "found": False, **edit_facts(old, new, "", kind)}
    row = rows[idx]
    at = row.find(old)
    prior = " ".join(rows.get(i) or "" for i in range(idx - _HEAD_ROWS, idx))
    around = (rows.get(idx - 1) or "", rows.get(idx + 1) or "")

    def _join(middle: str) -> str:
        return " ".join(part for part in (around[0], middle, around[1]) if part)

    return {
        "old": old,
        "new": new,
        "before": _join(row),
        "after": _join(row[:at] + new + row[at + len(old):]),
        "found": True,
        **edit_facts(old, new, prior + " " + row[:at], kind),
    }


def item_view(card: dict[str, Any], edits: list[dict[str, Any]], rows: dict[int, str],
              verses: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything the judge sees about one card, for live cards and gold alike."""
    changing = [e for e in edits if e["old"] != e["new"]]
    views = [_edit_view(rows, e, card.get("kind")) for e in changing]
    return {
        "key": card["key"],
        "chapter_id": card["chapter_id"],
        "ref": card.get("ref"),
        "kind": card.get("kind"),
        "adapted": bool(card.get("adapted")),
        "why": card.get("why") or "",
        "drift": card.get("drift") or "",
        "en_span": card.get("en_span") or "",
        "verses": [{"label": v.get("label"), "text": plain(v.get("text") or "")} for v in verses],
        "edits": views,
        "blocks": sorted({b for v in views for b in v["blocks"]}),
        "hash": edits_hash(changing),
    }


_PLACE_TEXT = {
    "inside": "inside « »",
    "epigraph": "an epigraph, set off like a quotation",
    "outside": "outside quotation marks",
}


def render_item(number: int, item: dict[str, Any]) -> str:
    head = f"Item {number}: {item['ref'] or 'no reference'}, {item['kind']}"
    if item["adapted"]:
        head += ", the scan says the author adapted it"
    lines = [head, f"  ENGLISH: {item['en_span']}"]
    if item["why"]:
        lines.append(f"  SCAN NOTE: {item['why']}")
    if item["drift"]:
        lines.append(f"  MAPPER'S DRIFT NOTE: {item['drift']}")
    for verse in item["verses"]:
        lines.append(f"  NBLA ({verse['label']}): {verse['text']}")
    for i, edit in enumerate(item["edits"], start=1):
        lines.append(f"  EDIT {i}: {_PLACE_TEXT[edit['place']]}; changes {edit['change'].replace('_', ' ')}")
        lines.append(f"    FROM: {edit['old']}")
        lines.append(f"    TO:   {edit['new']}")
        lines.append(f"    SENTENCE NOW: {edit['before']}")
        lines.append(f"    SENTENCE THEN: {edit['after']}")
    if item["blocks"]:
        lines.append("  CODE FOUND, so this cannot be apply: " + ", ".join(item["blocks"]))
    return "\n".join(lines)


# ── the prompt ───────────────────────────────────────────────────────────────

def load_examples() -> list[dict[str, Any]]:
    """The few-shot examples: the user's file (real marks, gitignored), else the
    tracked ``.example`` twin with invented wording."""
    for name in (EXAMPLES, EXAMPLES.replace(".json", ".example.json")):
        path = _PROMPTS_DIR / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return []


def render_examples(examples: list[dict[str, Any]]) -> str:
    blocks = []
    for ex in examples:
        answer = {"verdict": ex["verdict"], "confidence": ex["confidence"], "fix": ex.get("fix", ""),
                  "fix_kind": ex.get("fix_kind", ""), "reason": ex["reason"]}
        blocks.append(
            f"Example, {_PLACE_TEXT[ex['place']]}:\n"
            f"  FROM: {ex['old']}\n  TO:   {ex['new']}\n"
            f"  SENTENCE NOW: {ex['before']}\n  SENTENCE THEN: {ex['after']}\n"
            f"  ANSWER: {json.dumps(answer, ensure_ascii=False)}"
        )
    return "\n\n".join(blocks)


def prompt_parts(items: list[dict[str, Any]], examples: Optional[list[dict[str, Any]]] = None) -> tuple[str, str]:
    """``(preamble, body)`` for one job; the preamble is the same for every job in a run."""
    examples = load_examples() if examples is None else examples
    rendered = render(load_template(TEMPLATE), {
        "examples": render_examples(examples),
        "item_count": str(len(items)),
        # Last, so text inside an item is never itself scanned for a placeholder.
        "items": "\n\n".join(render_item(i, item) for i, item in enumerate(items, start=1)),
    })
    prefix, marker, suffix = rendered.partition(_CACHE_SPLIT)
    if not marker:
        raise ValueError(f"prompts/{TEMPLATE} has no cache split marker")
    return prefix, suffix.strip("\n")


# ── the wave, over any directory ─────────────────────────────────────────────

def _batches(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    """Jobs of at most ``size``, cut at chapter ends where that keeps them near full."""
    jobs: list[list[dict[str, Any]]] = []
    for item in items:
        if not jobs or len(jobs[-1]) >= size:
            jobs.append([])
        jobs[-1].append(item)
    return jobs


def write_jobs(
    root: Path,
    items: list[dict[str, Any]],
    *,
    project_dir: Path,
    worker_model: Optional[str] = None,
    cli: Optional[str] = None,
    items_per_job: int = DEFAULT_ITEMS_PER_JOB,
    keep_drafts: bool = False,
) -> dict[str, Any]:
    """Render the jobs and manifest into ``root`` (no spend)."""
    from src.harness.profile import resolve_profile, usage_log_for

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    prof = resolve_profile(
        project_dir, command=COMMAND, cli=cli, worker_model=worker_model,
        usage_log=usage_log_for(project_dir, COMMAND),
    )
    examples = load_examples()
    preamble = prompt_parts([], examples)[0]
    preamble_path = root / "preamble.txt"
    preamble_path.write_text(preamble, encoding="utf-8")
    jobs = []
    for n, batch in enumerate(_batches(items, items_per_job), start=1):
        job_id = f"job_{n:03d}"
        body_path, draft_path = root / f"{job_id}.body.txt", root / f"{job_id}.draft.json"
        body_path.write_text(prompt_parts(batch, examples)[1], encoding="utf-8")
        if not keep_drafts:
            draft_path.unlink(missing_ok=True)
        jobs.append({"id": job_id, "keys": [i["key"] for i in batch],
                     "body_path": str(body_path), "draft_path": str(draft_path)})
    (root / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest = {
        "project": Path(project_dir).name,
        "prepared_at": datetime.now().isoformat(),
        "prompt_version": prompt_version(TEMPLATE),
        "preamble_path": str(preamble_path),
        "worker_model": prof.worker_model,
        "cli": prof.cli,
        "effort": prof.effort,
        "jobs": jobs,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "jobs": len(jobs),
        "items": len(items),
        "body_chars": sum(Path(j["body_path"]).stat().st_size for j in jobs),
        "preamble_tokens_est": len(preamble) // 4,
        "manifest_path": str(manifest_path),
        "effective": prof.to_payload(),
    }


def run_jobs(
    root: Path,
    *,
    project_dir: Path,
    job_ids: Optional[list[str]] = None,
    concurrency: Optional[int] = None,
    cli: Optional[str] = None,
    cli_bin: Optional[str] = None,
    effort: Optional[str] = None,
    runner=None,
) -> dict[str, Any]:
    """One headless wave over the jobs in ``root``; a job with a draft is skipped."""
    root = Path(root)
    manifest, err = load_manifest(root / "manifest.json", missing="no judge manifest; run `judge-prepare` first")
    if err:
        return fanout_error(err)
    return run_wave(
        project_dir, manifest, command=COMMAND, job_ids=job_ids, concurrency=concurrency,
        cli=cli, cli_bin=cli_bin, effort=effort, runner=runner, root=root,
    )


def _item_number(value: Any) -> Optional[int]:
    from src.triage.pass_ import _item_number as number

    return number(value)


def _answers(raw: str) -> list[Any]:
    """The draft's objects: a JSON array, or the same objects one after another.

    Sonnet sometimes drops the brackets and writes one object per line. Every answer
    is still there, so that costs a parse, not a rerun of the job.
    """
    try:
        first = json.loads(extract_json(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise JudgeParseError(f"not JSON: {exc}") from exc
    if isinstance(first, list):
        return first
    if not isinstance(first, dict) or "item" not in first:
        raise JudgeParseError(f"expected a JSON array, got {type(first).__name__}")
    decoder = json.JSONDecoder()
    text, at, objects = raw.strip(), 0, []
    while at < len(text):
        try:
            obj, end = decoder.raw_decode(text, at)
        except json.JSONDecodeError as exc:
            raise JudgeParseError(f"not JSON: {exc}") from exc
        objects.append(obj)
        at = end
        while at < len(text) and text[at] in " \t\r\n,":
            at += 1
    return objects


def parse_draft(raw: str, keys: list[str]) -> dict[str, dict[str, Any]]:
    """``{key: verdict}`` from one job's draft; every item must be answered exactly once.

    The draft answers by position, as triage's does: a model cannot copy a card key
    back reliably, so ``keys`` must stay in the order the prompt rendered them.

    Raises:
        JudgeParseError: not a JSON array, an unknown verdict, a confidence that is
            not a number in [0, 1], or item numbers that are not exactly the job's.
    """
    data = _answers(raw)
    out: dict[int, dict[str, Any]] = {}
    for n, obj in enumerate(data):
        if not isinstance(obj, dict):
            raise JudgeParseError(f"entry {n} is not an object")
        number = _item_number(obj.get("item"))
        if number is None:
            raise JudgeParseError(f"entry {n}: item {obj.get('item')!r} is not a whole number")
        if number in out:
            raise JudgeParseError(f"item {number} appears more than once")
        verdict = str(obj.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            raise JudgeParseError(f"item {number}: verdict {obj.get('verdict')!r} is not one of {list(VERDICTS)}")
        raw_conf = obj.get("confidence")
        # bool subclasses int, so float(True) would read as full confidence.
        if isinstance(raw_conf, bool) or not isinstance(raw_conf, (int, float)) or not 0.0 <= raw_conf <= 1.0:
            raise JudgeParseError(f"item {number}: confidence {raw_conf!r} is not a number in [0, 1]")
        fix_kind = str(obj.get("fix_kind") or "").strip().lower()
        out[number] = {
            "verdict": verdict,
            "confidence": float(raw_conf),
            "fix": str(obj.get("fix") or "").strip(),
            "fix_kind": fix_kind if fix_kind in FIX_KINDS else "",
            "reason": str(obj.get("reason") or "").strip(),
        }
    expected = set(range(1, len(keys) + 1))
    if set(out) != expected:
        raise JudgeParseError(
            f"item numbers do not match the job: missing {sorted(expected - set(out))}, "
            f"unexpected {sorted(set(out) - expected)}"
        )
    return {keys[n - 1]: v for n, v in out.items()}


def clamp(verdict: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """A card with a coded defect cannot be ``apply``: it becomes ``partial``."""
    out = {**verdict, "blocks": list(item.get("blocks") or []), "hash": item["hash"]}
    if verdict["verdict"] == VERDICT_APPLY and out["blocks"]:
        out["verdict"] = VERDICT_PARTIAL
        out["model_verdict"] = VERDICT_APPLY
        out["fix"] = out["fix"] or " ".join(_BLOCK_FIX[b] for b in out["blocks"])
        out["fix_kind"] = out["fix_kind"] or "repair"
    return out


def collect(root: Path) -> dict[str, Any]:
    """Read the drafts back: ``{verdicts, failed, missing, manifest}``."""
    root = Path(root)
    manifest, err = load_manifest(root / "manifest.json", missing="no judge manifest; run `judge-prepare` first")
    if err:
        return {"status": "error", "error": err}
    items = {i["key"]: i for i in json.loads((root / "items.json").read_text(encoding="utf-8"))}
    verdicts: dict[str, dict[str, Any]] = {}
    failed: list[dict[str, str]] = []
    missing: list[str] = []
    for job in manifest["jobs"]:
        draft = Path(job["draft_path"])
        if not draft.exists():
            missing.append(job["id"])
            continue
        try:
            parsed = parse_draft(draft.read_text(encoding="utf-8"), job["keys"])
        except (OSError, UnicodeDecodeError, JudgeParseError) as exc:
            failed.append({"id": job["id"], "problem": str(exc)[:500]})
            continue
        verdicts.update({key: clamp(v, items[key]) for key, v in parsed.items()})
    return {"status": "partial" if failed or missing else "ok", "verdicts": verdicts,
            "failed": failed, "missing": missing, "manifest": manifest}


# ── a live pass on one book ──────────────────────────────────────────────────

def _chapter_rows(project_dir: Path, chapter_id: str) -> dict[int, str]:
    return {r["es_idx"]: r.get("es") or "" for r in qscan.load_rows(project_dir, chapter_id) if "es_idx" in r}


def live_items(project_dir: Path, chapters: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Every unapplied card that would change the text, in card order."""
    from src.quotations.apply import finished_cards
    from src.quotations.board import load_mapped

    project_dir = Path(project_dir)
    verses = {c["key"]: c.get("verses") or [] for c in (qmap.load_resolved(project_dir) or {}).get("candidates") or []}
    applied = finished_cards(project_dir)
    rows: dict[str, dict[int, str]] = {}
    items = []
    for card in (load_mapped(project_dir) or {}).get("cards") or []:
        if card["key"] in applied or card.get("status") != qmap.CARD_PROPOSED:
            continue
        if chapters and card["chapter_id"] not in chapters:
            continue
        if not any(e["old"] != e["new"] for e in card.get("quote_edits") or []):
            continue
        chapter_rows = rows.setdefault(card["chapter_id"], _chapter_rows(project_dir, card["chapter_id"]))
        items.append(item_view(card, card["quote_edits"], chapter_rows, verses.get(card["key"]) or []))
    return items


def judge_prepare(
    project_dir: Path,
    *,
    chapters: Optional[list[str]] = None,
    worker_model: Optional[str] = None,
    cli: Optional[str] = None,
    items_per_job: int = DEFAULT_ITEMS_PER_JOB,
    keep_drafts: bool = False,
) -> dict[str, Any]:
    project_dir = Path(project_dir)
    items = live_items(project_dir, chapters)
    if not items:
        return {"status": "error", "error": "no unapplied card changes the text in those chapters"}
    return {
        **write_jobs(judge_dir(project_dir), items, project_dir=project_dir, worker_model=worker_model,
                     cli=cli, items_per_job=items_per_job, keep_drafts=keep_drafts),
        "instructions": "Quote jobs, items and `effective` at the usage gate. Then `judge-fanout`, `judge-commit`.",
    }


def judge_fanout(project_dir: Path, **kwargs: Any) -> dict[str, Any]:
    project_dir = Path(project_dir)
    return run_jobs(judge_dir(project_dir), project_dir=project_dir, **kwargs)


def load_verdicts(project_dir: Path) -> dict[str, Any]:
    try:
        doc = json.loads(verdicts_path(Path(project_dir)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"verdicts": {}}
    return doc if isinstance(doc, dict) and isinstance(doc.get("verdicts"), dict) else {"verdicts": {}}


def verdict_for(card: dict[str, Any], doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The card's verdict, unless its edits have changed since: then there is none."""
    verdict = doc.get("verdicts", {}).get(card["key"])
    if not verdict or verdict.get("hash") != edits_hash(card.get("quote_edits") or []):
        return None
    return verdict


def judge_commit(project_dir: Path) -> dict[str, Any]:
    """Merge the drafts into ``judge.json``. Verdicts from an earlier wave are kept."""
    project_dir = Path(project_dir)
    got = collect(judge_dir(project_dir))
    if got["status"] == "error":
        return got
    manifest = got["manifest"]
    doc = load_verdicts(project_dir)
    doc["verdicts"].update(got["verdicts"])
    doc.update({k: manifest.get(k) for k in ("prompt_version", "worker_model", "cli", "effort")})
    doc["committed_at"] = datetime.now().isoformat(timespec="seconds")
    verdicts_path(project_dir).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    counts: dict[str, int] = {}
    for verdict in got["verdicts"].values():
        counts[verdict["verdict"]] = counts.get(verdict["verdict"], 0) + 1
    return {
        "status": got["status"],
        "counts": counts,
        "failed": got["failed"],
        "missing": got["missing"],
        "verdicts_path": str(verdicts_path(project_dir)),
        "instructions": "Re-run `judge-fanout` for anything failed or missing, then commit again. "
                        "Otherwise open the board.",
    }
