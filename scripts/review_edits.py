"""Generate an HTML diff report comparing each chunk's current translation
against the most recent raw LLM output that produced it.

Reads:
    projects/<id>/chunks/*.json   (current state)
    prompts/history/*.json        (raw LLM responses; baseline — every
                                   completed translation log carries both
                                   prompt and response regardless of mode)

Writes:
    projects/<id>/reports/edits-<scope>-<timestamp>.html

Baseline resolution per chunk:
    1. chunk.last_llm_log -> direct path under prompts/history
    2. Fallback: most recent prompts/history file whose metadata.chunk_id
       matches and that has a non-null response

The HTML report uses tight char windows around the translation diff and a
deliberately wider window for the source, with text outside the proportionally
mapped region dimmed (alignment is approximate, so show more not less).

Open the report via http://localhost:5000/reports/<project_id>/<filename>
so that the embedded tag buttons (which POST to /api/edit-tag) are same-origin.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

from src.edit_review_constants import EDIT_TAGS  # noqa: E402

# Tunables
TRANSLATION_CONTEXT_CHARS = 120
SOURCE_CONTEXT_CHARS = 400
MERGE_HUNK_GAP_CHARS = 40


@dataclass
class Baseline:
    response: str
    prompt: str
    source_text: str
    log_path: Path
    log_timestamp: str


# ----- IO helpers -----------------------------------------------------------


def _projects_dir() -> Path:
    return _REPO_ROOT / "projects"


def _history_dir() -> Path:
    return _REPO_ROOT / "prompts" / "history"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_source_from_prompt(prompt_text: str) -> str:
    """Extract the source body from a prompt log's `prompt` field.

    Layout assumed (from prompts/translation.txt template):
        ===<bar>===
        SOURCE TEXT TO TRANSLATE
        ===<bar>===
        <source body, possibly multiple paragraphs>
        ===<bar>===
        GLOSSARY TERMS
        ...
    """
    marker = "SOURCE TEXT TO TRANSLATE"
    idx = prompt_text.find(marker)
    if idx < 0:
        return ""
    after_marker = prompt_text[idx + len(marker):]
    bar = "=" * 10
    bar_idx = after_marker.find(bar)
    if bar_idx < 0:
        return ""
    after_bar = after_marker[bar_idx:]
    nl_idx = after_bar.find("\n")
    if nl_idx < 0:
        return ""
    body = after_bar[nl_idx + 1:]
    end_idx = body.find("=" * 80)
    if end_idx < 0:
        return body.strip()
    return body[:end_idx].strip()


def _build_history_index() -> dict[str, list[tuple[str, Path]]]:
    """Map chunk_id -> list of (timestamp, path) for translation logs with a
    non-null response, sorted newest-first."""
    index: dict[str, list[tuple[str, Path]]] = {}
    history_dir = _history_dir()
    if not history_dir.exists():
        return index
    for path in history_dir.glob("*_translation*.json"):
        try:
            doc = _load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        meta = doc.get("metadata") or {}
        chunk_id = meta.get("chunk_id")
        if not chunk_id:
            continue
        if meta.get("call_type") != "translation":
            continue
        if doc.get("response") is None:
            continue
        ts = meta.get("timestamp", "")
        index.setdefault(chunk_id, []).append((ts, path))
    for entries in index.values():
        entries.sort(key=lambda x: x[0], reverse=True)
    return index


def _normalize_for_match(text: str) -> str:
    """Aggressive normalization for cross-project source-text matching."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _source_texts_match(prompt_source: str, chunk_source: str) -> bool:
    """Cheap sanity check: do the first ~200 non-ws chars of source text agree?

    Used in the fallback baseline path to avoid matching a same-`chunk_id` log
    from a different project (prompts/history doesn't carry project_id).
    """
    a = _normalize_for_match(prompt_source)
    b = _normalize_for_match(chunk_source)
    if not a or not b:
        return False
    head = 200
    return a[:head] == b[:head] or a[:head] in b[: head * 4] or b[:head] in a[: head * 4]


def _build_baseline_from_log(log_path: Path) -> Optional[Baseline]:
    try:
        doc = _load_json(log_path)
    except (json.JSONDecodeError, OSError):
        return None
    if doc.get("response") is None:
        return None
    prompt = doc.get("prompt", "")
    return Baseline(
        response=doc["response"],
        prompt=prompt,
        source_text=_parse_source_from_prompt(prompt),
        log_path=log_path,
        log_timestamp=doc.get("metadata", {}).get("timestamp", ""),
    )


def _resolve_baseline(chunk: dict, history_index: dict[str, list[tuple[str, Path]]]) -> Optional[Baseline]:
    # Path A: provenance stamp (trusted — written at LLM-call time)
    rel = chunk.get("last_llm_log")
    if rel:
        candidate = _REPO_ROOT / rel
        if candidate.exists():
            b = _build_baseline_from_log(candidate)
            if b is not None:
                return b

    # Path B: fallback scan, newest-first, with source-text sanity check.
    # Every log now carries the original prompt (batch retrieval mutates the
    # submission log in place rather than writing a separate result log), so
    # the sanity check is enough to gate cross-project chunk_id collisions.
    chunk_source = chunk.get("source_text") or ""
    for _ts, log_path in history_index.get(chunk.get("id", ""), []):
        b = _build_baseline_from_log(log_path)
        if b is None or not b.source_text:
            continue
        if _source_texts_match(b.source_text, chunk_source):
            return b
    return None


# ----- Diff machinery -------------------------------------------------------


_TOKEN_RE = re.compile(r"\S+|\s+")


def _tokenize_with_offsets(text: str) -> tuple[list[str], list[int]]:
    tokens = _TOKEN_RE.findall(text)
    offsets = [0]
    for tok in tokens:
        offsets.append(offsets[-1] + len(tok))
    return tokens, offsets


def _opcode_hunks(a: str, b: str) -> list[dict]:
    """Word-level diff. Returns non-equal opcodes with char ranges in both
    strings."""
    a_tok, a_off = _tokenize_with_offsets(a)
    b_tok, b_off = _tokenize_with_offsets(b)
    sm = difflib.SequenceMatcher(a=a_tok, b=b_tok, autojunk=False)
    hunks: list[dict] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        hunks.append({
            "op": op,
            "a_start": a_off[i1],
            "a_end": a_off[i2],
            "b_start": b_off[j1],
            "b_end": b_off[j2],
        })
    return hunks


def _merge_close_hunks(hunks: list[dict]) -> list[dict]:
    if not hunks:
        return hunks
    merged = [dict(hunks[0])]
    for h in hunks[1:]:
        prev = merged[-1]
        if h["b_start"] - prev["b_end"] <= MERGE_HUNK_GAP_CHARS:
            prev["b_end"] = h["b_end"]
            prev["a_end"] = h["a_end"]
            prev["op"] = "replace"
        else:
            merged.append(dict(h))
    return merged


def _render_hunk_side(
    window_text: str,
    this_hunk_text: str,
    hl_start: int,
    hl_end: int,
    other_hunk_text: str,
    kind: str,
) -> str:
    """Render `window_text` HTML-escaped, with only [hl_start:hl_end]
    word-diff-highlighted against `other_hunk_text`.

    Everything outside the hunk range renders plain — this is what keeps
    adjacent hunks' changes from bleeding into this hunk's display. Within
    the hunk range, a fresh SequenceMatcher run on just the two hunk
    substrings yields word-level granularity (so a merged "replace" range
    with internal equal tokens stays clean).

    `kind` is 'del' for the baseline side or 'ins' for the current side.
    When `hl_start == hl_end` (the no-character side of an insert/delete op),
    the window renders fully plain.
    """
    prefix = html.escape(window_text[:hl_start])
    suffix = html.escape(window_text[hl_end:])
    if hl_start == hl_end or not this_hunk_text:
        return prefix + suffix

    this_tok, _ = _tokenize_with_offsets(this_hunk_text)
    other_tok, _ = _tokenize_with_offsets(other_hunk_text)
    sm = difflib.SequenceMatcher(a=this_tok, b=other_tok, autojunk=False)
    parts: list[str] = []
    for op, i1, i2, _j1, _j2 in sm.get_opcodes():
        chunk = "".join(this_tok[i1:i2])
        if not chunk:
            continue
        escaped = html.escape(chunk)
        if op == "equal":
            parts.append(escaped)
        else:
            parts.append(f'<span class="{kind}">{escaped}</span>')
    return prefix + "".join(parts) + suffix


def _windowed(text: str, start: int, end: int) -> tuple[str, bool, bool]:
    """Clamp [start, end] to len(text); return (slice, truncated_left, truncated_right)."""
    n = len(text)
    s = max(0, start)
    e = min(n, end)
    return text[s:e], s > 0, e < n


def _proportional_source_range(
    b_start: int, b_end: int, cur_trans_len: int, src_len: int
) -> tuple[int, int]:
    if cur_trans_len <= 0 or src_len <= 0:
        return 0, src_len
    s = int(b_start / cur_trans_len * src_len)
    e = int(b_end / cur_trans_len * src_len)
    s = max(0, min(src_len, s))
    e = max(s, min(src_len, e))
    return s, e


# Sentence terminator: Western + ellipsis + CJK punctuation, optionally followed
# by a closing quote/paren, then whitespace or end-of-string. `\n` is a hard
# boundary on its own (handles list items, headings, blank lines).
_SENT_TERMINATOR_RE = re.compile(
    r'[.!?…。！？][)\]"\'»”’]?(?=\s|\Z)|\n'
)


def _expand_to_sentence(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand [start, end] outward to the nearest sentence boundaries.

    Used to make the source-pane highlight at least a full sentence, even
    when the proportional anchor lands on a single word. Falls back to the
    enclosing text bounds when no terminator is found on a side (graceful
    degradation for languages without these punctuation marks).
    """
    n = len(text)
    if n == 0:
        return 0, 0
    matches = list(_SENT_TERMINATOR_RE.finditer(text))
    new_start = 0
    for m in matches:
        if m.end() <= start:
            new_start = m.end()
        else:
            break
    while new_start < n and text[new_start] in " \t\r\n":
        new_start += 1
    new_end = n
    for m in matches:
        if m.start() >= end:
            new_end = m.end()
            break
    return min(new_start, start), max(new_end, end)


def _build_source_spans(
    source: str, inside_start: int, inside_end: int, window_chars: int
) -> tuple[list[dict], bool, bool]:
    """Build [(text, kind)] spans for the source pane. kind is 'dim' (outside
    proportional region) or 'inside' (proportional region)."""
    n = len(source)
    win_start = max(0, inside_start - window_chars)
    win_end = min(n, inside_end + window_chars)

    spans: list[dict] = []
    if win_start < inside_start:
        spans.append({"text": source[win_start:inside_start], "kind": "dim"})
    spans.append({"text": source[inside_start:inside_end], "kind": "inside"})
    if inside_end < win_end:
        spans.append({"text": source[inside_end:win_end], "kind": "dim"})
    return spans, win_start > 0, win_end < n


# ----- Tag log loading ------------------------------------------------------


def _load_tags(project_dir: Path) -> dict[tuple[str, int], list[dict]]:
    """Read projects/<id>/edit_review_tags.jsonl. Returns {(chunk_id, hunk_index): [rows]}."""
    out: dict[tuple[str, int], list[dict]] = {}
    p = project_dir / "edit_review_tags.jsonl"
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                hunk_idx = int(row.get("hunk_index", -1))
            except (ValueError, TypeError):
                continue
            key = (row.get("chunk_id", ""), hunk_idx)
            if key[1] < 0:
                continue
            out.setdefault(key, []).append(row)
    return out


# ----- Per-chunk processing -------------------------------------------------


def _process_chunk(
    chunk_path: Path,
    history_index: dict,
    tag_map: dict[tuple[str, int], list[dict]],
) -> Optional[dict]:
    chunk = _load_json(chunk_path)
    chunk_id = chunk.get("id") or chunk_path.stem
    current_trans = (chunk.get("translated_text") or "").strip()
    current_src = chunk.get("source_text") or ""

    baseline = _resolve_baseline(chunk, history_index)

    if baseline is None:
        if not current_trans:
            return None  # untranslated, skip
        return {
            "chunk_id": chunk_id,
            "chapter_id": chunk.get("chapter_id", ""),
            "position": chunk.get("position", 0),
            "no_baseline": True,
            "hunks": [],
            "source_changed": False,
        }

    baseline_trans = baseline.response.strip()
    if not current_trans or current_trans == baseline_trans:
        return None  # no edit

    raw_hunks = _opcode_hunks(baseline_trans, current_trans)
    hunks = _merge_close_hunks(raw_hunks)
    if not hunks:
        return None

    rendered: list[dict] = []
    for idx, h in enumerate(hunks):
        # Translation window
        b_win_start = max(0, h["b_start"] - TRANSLATION_CONTEXT_CHARS)
        b_win_end = min(len(current_trans), h["b_end"] + TRANSLATION_CONTEXT_CHARS)
        a_win_start = max(0, h["a_start"] - TRANSLATION_CONTEXT_CHARS)
        a_win_end = min(len(baseline_trans), h["a_end"] + TRANSLATION_CONTEXT_CHARS)

        a_slice = baseline_trans[a_win_start:a_win_end]
        b_slice = current_trans[b_win_start:b_win_end]

        a_local_start = h["a_start"] - a_win_start
        a_local_end = h["a_end"] - a_win_start
        b_local_start = h["b_start"] - b_win_start
        b_local_end = h["b_end"] - b_win_start
        a_hunk_text = baseline_trans[h["a_start"]:h["a_end"]]
        b_hunk_text = current_trans[h["b_start"]:h["b_end"]]

        a_html = _render_hunk_side(
            a_slice, a_hunk_text, a_local_start, a_local_end, b_hunk_text, "del"
        )
        b_html = _render_hunk_side(
            b_slice, b_hunk_text, b_local_start, b_local_end, a_hunk_text, "ins"
        )

        # Source window — proportional anchor, then snap outward to sentence
        src_inside_start, src_inside_end = _proportional_source_range(
            h["b_start"], h["b_end"], max(1, len(current_trans)), len(current_src)
        )
        src_inside_start, src_inside_end = _expand_to_sentence(
            current_src, src_inside_start, src_inside_end
        )
        spans, src_trunc_l, src_trunc_r = _build_source_spans(
            current_src, src_inside_start, src_inside_end, SOURCE_CONTEXT_CHARS
        )

        tag_rows = tag_map.get((chunk_id, idx), [])
        # Collapse repeated tags
        unique_tags: list[str] = []
        seen = set()
        for row in tag_rows:
            t = row.get("tag")
            if t and t not in seen:
                unique_tags.append(t)
                seen.add(t)

        rendered.append({
            "hunk_index": idx,
            "op": h["op"],
            "baseline_html": a_html,
            "current_html": b_html,
            "trans_truncated_left": a_win_start > 0 or b_win_start > 0,
            "trans_truncated_right": a_win_end < len(baseline_trans) or b_win_end < len(current_trans),
            "source_spans": spans,
            "source_truncated_left": src_trunc_l,
            "source_truncated_right": src_trunc_r,
            "tags": tag_rows,
            "unique_tag_set": unique_tags,
        })

    source_changed = baseline.source_text.strip() != current_src.strip() if baseline.source_text else False

    return {
        "chunk_id": chunk_id,
        "chapter_id": chunk.get("chapter_id", ""),
        "position": chunk.get("position", 0),
        "no_baseline": False,
        "source_changed": source_changed,
        "baseline_log": str(baseline.log_path.relative_to(_REPO_ROOT).as_posix()),
        "baseline_timestamp": baseline.log_timestamp,
        "hunks": rendered,
    }


# ----- Report assembly ------------------------------------------------------


def _collect_chunks(project_dir: Path, chapter_filter: Optional[str]) -> list[Path]:
    chunks_dir = project_dir / "chunks"
    pattern = f"{chapter_filter}_chunk_*.json" if chapter_filter else "*_chunk_*.json"
    return sorted(chunks_dir.glob(pattern))


def build_report(
    project_id: str,
    chapter_filter: Optional[str],
    output_dir: Optional[Path] = None,
) -> Path:
    project_dir = _projects_dir() / project_id
    if not project_dir.exists():
        raise SystemExit(f"Project not found: {project_dir}")

    chunk_paths = _collect_chunks(project_dir, chapter_filter)
    if not chunk_paths:
        scope = f"chapter {chapter_filter}" if chapter_filter else "any chapter"
        raise SystemExit(f"No chunks in {scope} of {project_id}")

    history_index = _build_history_index()
    tag_map = _load_tags(project_dir)

    chunk_reports: list[dict] = []
    for chunk_path in chunk_paths:
        try:
            report = _process_chunk(chunk_path, history_index, tag_map)
        except Exception as e:  # never let one bad chunk kill the run
            print(f"WARN: failed to process {chunk_path.name}: {e}", file=sys.stderr)
            continue
        if report is not None:
            chunk_reports.append(report)

    edited_count = sum(1 for c in chunk_reports if not c["no_baseline"])
    no_baseline_count = sum(1 for c in chunk_reports if c["no_baseline"])

    chapters: list[dict] = []
    chapter_index: dict[str, int] = {}
    seen_chapter: set[str] = set()
    for c in chunk_reports:
        cid = c.get("chapter_id") or ""
        c["is_chapter_start"] = cid not in seen_chapter
        if cid not in chapter_index:
            chapter_index[cid] = len(chapters)
            chapters.append({
                "chapter_id": cid,
                "anchor_id": f"chapter-{cid}" if cid else "chapter-_unknown",
                "edit_count": 0,
                "no_baseline_count": 0,
            })
        entry = chapters[chapter_index[cid]]
        entry["edit_count"] += len(c.get("hunks") or [])
        if c.get("no_baseline"):
            entry["no_baseline_count"] += 1
        seen_chapter.add(cid)

    # Render
    templates_dir = _REPO_ROOT / "web_ui" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(enabled_extensions=("html", "j2", "html.j2")),
    )
    # Disable autoescape inside specific filters via |safe in the template
    template = env.get_template("edit_review_report.html.j2")

    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    scope_label = chapter_filter or "all"
    rendered_html = template.render(
        project_id=project_id,
        scope_label=scope_label,
        generated_at=now.isoformat(timespec="seconds"),
        chunks=chunk_reports,
        chapters=chapters,
        edited_count=edited_count,
        no_baseline_count=no_baseline_count,
        edit_tags=EDIT_TAGS,
    )

    out_dir = output_dir or (project_dir / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"edits-{scope_label}-{timestamp}.html"
    out_path = out_dir / filename
    out_path.write_text(rendered_html, encoding="utf-8")
    return out_path


# ----- CLI ------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True, help="Project id (subdir of projects/)")
    parser.add_argument("--chapter", default=None, help="Chapter id (e.g. chapter_03). Default: all chapters.")
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory. Default: projects/<id>/reports/",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the report in the default browser via http://localhost:5000/reports/...",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port the Flask web UI is running on (used with --open). Default: 5000.",
    )
    args = parser.parse_args()

    out_dir = Path(args.output) if args.output else None
    out_path = build_report(args.project, args.chapter, out_dir)
    print(f"Wrote {out_path}")

    if args.open:
        url = f"http://localhost:{args.port}/reports/{args.project}/{out_path.name}"
        print(f"Opening {url}")
        webbrowser.open(url)


if __name__ == "__main__":
    main()
