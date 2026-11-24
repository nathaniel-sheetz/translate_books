"""
Figures added to a book after its text: pictures with captions, put into the
English source and the translation together.

A book whose text came without its pictures (a Gutenberg transcription that
left the plates out) can get them later from a scan: ``ocrt plates`` in the
sister project ocr_texts cuts the plates out of an Archive scan and reads their
captions into ``out/plates.json``. This module takes them from there into a
book that may already be translated.

The plan lives in ``<project>/figures.json``, one entry per figure:

    id            the picture's name (``leaf035_1``)
    src           where the picture is now (absolute path)
    image         the file it becomes under the project: ``images/plate_<id>.jpg``
    captions_en   the printed captions, one per bird (or other subject)
    captions_es   their translation, same count; the agent drafts them, the board edits them
    alt_en/alt_es the short names for the image's alt text
    chapter_id    the chapter it goes in
    anchor_en     the opening of the English paragraph it goes after (unique in its chunk)
    status        draft -> approved -> inserted (``removed`` after a remove)

Placement goes after a paragraph, never inside one. In the chunk, the English
side is found by ``anchor_en``; the Spanish side is the paragraph holding the
last Spanish sentence aligned to that English paragraph (the translation splits
dialogue into more paragraphs than the English has, so paragraph numbers do not
carry over). Every figure is planned before anything is written, and a figure
that cannot be placed stops the batch with the reason.

Writes, per chunk touched: a ``.chunk_edits/`` snapshot, ``source_text`` and
``translated_text``, the evaluation's stale stamp; then ``source.txt``, the image
under ``images/``, the chapter's recombine and realign (which re-anchors reader
notes), and the plan's ``inserted`` status. ``remove`` takes a figure out again
the same way. Image blocks and their captions are the format ingest writes:
``[IMAGE:images/<file>:<alt>]`` then one ``[CAPTION]`` block per caption.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PLAN_FILE = "figures.json"
PREFIX = "plate_"
ANCHOR_CHARS = 120

_BLOCK_SEP_RE = re.compile(r"\n\s*\n")
_IMAGE_RE = re.compile(r"^\[IMAGE:(images/[^:\]]+)(?::[^\]]*)?\]$")


# ── plan file ───────────────────────────────────────────────────────────────


def plan_path(project_dir: Path) -> Path:
    return Path(project_dir) / PLAN_FILE


def load_plan(project_dir: Path) -> dict:
    path = plan_path(project_dir)
    if not path.exists():
        return {"version": 1, "figures": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_plan(project_dir: Path, plan: dict) -> Path:
    path = plan_path(project_dir)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def _by_id(plan: dict) -> dict[str, dict]:
    return {f["id"]: f for f in plan.get("figures", [])}


# ── text helpers ────────────────────────────────────────────────────────────


def blocks(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, block)`` for each blank-line-separated block; offsets into
    *text* at the block's first and one past its last non-space character."""
    out, pos = [], 0
    for sep in list(_BLOCK_SEP_RE.finditer(text)) + [None]:
        stop = sep.start() if sep else len(text)
        seg = text[pos:stop]
        if seg.strip():
            lead = len(seg) - len(seg.lstrip())
            out.append((pos + lead, pos + len(seg.rstrip()), seg.strip()))
        if sep:
            pos = sep.end()
    return out


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()


def _anchor_of(block: str) -> str:
    return _fold(block)[:ANCHOR_CHARS]


def find_anchor(text: str, anchor: str) -> list[tuple[int, int, str]]:
    """The blocks of *text* that open with *anchor* (folded)."""
    a = _fold(anchor)
    return [b for b in blocks(text) if _fold(b[2]).startswith(a)]


def caption_name(caption: str) -> str:
    """The name a printed caption opens with, in capitals up to its first stop
    or comma: ``JENNY WREN. This is ...`` -> ``JENNY WREN``."""
    m = re.match(r"^\s*(.+?)[.,](\s|$)", caption)
    return (m.group(1) if m else caption).strip()


def _title(name: str) -> str:
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), name.lower())


def bird_terms(caption: str) -> tuple[Optional[str], Optional[str]]:
    """``BULLY THE ENGLISH SPARROW`` -> (``Bully``, ``English Sparrow``);
    ``JENNY WREN`` -> (``Jenny Wren``, None); ``THE MALE COWBIRD`` -> (None,
    ``Male Cowbird``)."""
    name = caption_name(caption)
    if _fold(name).upper() != _fold(name):  # not a name in capitals
        return None, None
    m = re.match(r"^(?:(.*?)\s+)?THE\s+(.+)$", name)
    if m:
        nick = _title(m.group(1)) if m.group(1) else None
        return nick, _title(m.group(2))
    return _title(name), None


def _term_re(term: str, case: bool) -> re.Pattern:
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", term) if p]
    # SWEET VOICE matches Sweetvoice, DEAR ME matches Dear-Me
    return re.compile(r"\b" + r"[\s\-]*".join(parts) + r"\b", 0 if case else re.I)


# ── chunks and chapters ─────────────────────────────────────────────────────


def _chunk_paths(project_dir: Path) -> list[Path]:
    def key(p: Path):
        m = re.match(r"chapter_(\d+)_chunk_(\d+)", p.stem)
        return (int(m.group(1)), int(m.group(2))) if m else (10**9, 0)
    return sorted((Path(project_dir) / "chunks").glob("chapter_*_chunk_*.json"), key=key)


def _chunk_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def chapters_en(project_dir: Path) -> list[tuple[str, list[tuple[str, str]]]]:
    """``[(chapter_id, [(chunk_id, source_text), ...]), ...]`` in book order."""
    out: list[tuple[str, list[tuple[str, str]]]] = []
    for p in _chunk_paths(project_dir):
        c = _chunk_json(p)
        chapter = c.get("chapter_id") or p.stem.rsplit("_chunk_", 1)[0]
        if not out or out[-1][0] != chapter:
            out.append((chapter, []))
        out[-1][1].append((p.stem, c.get("source_text") or ""))
    return out


def suggest_place(project_dir: Path, captions_en: list[str],
                  book: Optional[list] = None, taken: Optional[set] = None) -> dict:
    """Where a figure goes: the chapter about its subject, after the first
    paragraph there that names it.

    Each caption's subject is searched by its nickname (case kept: "Bully",
    "Jenny Wren") and its kind ("English Sparrow", any case). The chapter about
    it is the first whose heading names it ("Longbill and Teeter"), else the
    one that names it most: a bird named in passing early on does not pull its
    picture there. With two subjects in one picture, the one whose chapter
    comes first places it. A paragraph another figure has already ``taken``
    is passed over for the next one naming the subject."""
    book = book if book is not None else chapters_en(project_dir)
    best: Optional[dict] = None
    for cap in captions_en:
        nick, kind = bird_terms(cap)
        pats = []
        if nick:
            pats.append(_term_re(nick, case=True))
        if kind:
            pats.append(_term_re(kind, case=False))
            last = kind.split()[-1]
            if len(kind.split()) > 1 and len(last) > 4:
                pats.append(_term_re(last, case=False))
        if not pats:
            continue
        counts, headed = [], None
        for i, (chapter, chunks) in enumerate(book):
            text = "\n\n".join(t for _, t in chunks)
            first = blocks(chunks[0][1]) if chunks else []
            if headed is None and first and any(p.search(first[0][2]) for p in pats[:2]):
                headed = i
            counts.append(sum(len(p.findall(text)) for p in pats[:2]) or
                          (len(pats[2].findall(text)) if len(pats) > 2 else 0))
        top = max(counts) if counts else 0
        if not top:
            continue
        ci = headed if headed is not None else counts.index(top)
        chapter, chunks = book[ci]
        for chunk_id, text in chunks:
            hit = None
            for k, (_, _, blk) in enumerate(blocks(text)):
                if k == 0 or blk.startswith("[") or (chapter, _anchor_of(blk)) in (taken or ()):
                    continue  # the heading; an image or caption; another figure's place
                if any(p.search(blk) for p in pats):
                    hit = blk
                    break
            if hit:
                cand = {"chapter_id": chapter, "chunk_id": chunk_id,
                        "anchor_en": _anchor_of(hit), "subject": nick or kind,
                        "chapter_index": ci, "counts_top": top}
                if best is None or ci < best["chapter_index"]:
                    best = cand
                break
    return best or {}


# ── import ──────────────────────────────────────────────────────────────────


def _read_source(path: Path) -> list[dict]:
    """Figures offered by a plates file: ocr_texts' ``plates.json``
    (``{"plates": [{name, image, captions: [{text, doubtful}]}]}``), or a plain
    list of ``{name, image, captions: [str]}``. Image paths are relative to
    the file's folder."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("plates", []) if isinstance(data, dict) else data
    out = []
    for r in rows:
        caps = r.get("captions", [])
        texts = [c["text"] if isinstance(c, dict) else str(c) for c in caps]
        doubtful = any(isinstance(c, dict) and c.get("doubtful") for c in caps)
        img = Path(r["image"])
        out.append({"id": r.get("name") or img.stem, "src": str((path.parent / img).resolve()),
                    "captions_en": texts, "doubtful_en": doubtful,
                    "leaf": r.get("leaf"), "facing_page": r.get("facing_page")})
    return out


def import_figures(project_dir: Path, source: Path) -> dict:
    """Add (or refresh) the plan's figures from a plates file, with a suggested
    place for each new one. An entry already in the plan keeps its translation,
    place and status; only its picture and English captions are refreshed, and
    never once it is inserted."""
    project_dir = Path(project_dir)
    plan = load_plan(project_dir)
    have = _by_id(plan)
    book = chapters_en(project_dir)
    added, refreshed, unplaced = [], [], []
    taken = {(f.get("chapter_id"), f.get("anchor_en")) for f in plan.get("figures", [])}
    for row in _read_source(Path(source)):
        fig = have.get(row["id"])
        if fig and fig.get("status") == "inserted":
            continue
        if fig:
            fig.update({k: row[k] for k in ("src", "captions_en", "doubtful_en",
                                             "leaf", "facing_page")})
            refreshed.append(row["id"])
            continue
        place = suggest_place(project_dir, row["captions_en"], book, taken)
        taken.add((place.get("chapter_id"), place.get("anchor_en")))
        names = [caption_name(c) for c in row["captions_en"]]
        fig = {
            **row, "image": f"images/{PREFIX}{row['id']}.jpg",
            "captions_es": [], "alt_en": " / ".join(_title(n) for n in names), "alt_es": "",
            "chapter_id": place.get("chapter_id"), "chunk_id": place.get("chunk_id"),
            "anchor_en": place.get("anchor_en"), "subject": place.get("subject"),
            "status": "draft",
        }
        if not place:
            unplaced.append(row["id"])
        plan.setdefault("figures", []).append(fig)
        added.append(row["id"])
    plan["source"] = str(Path(source).resolve())
    _sort(plan, book)
    save_plan(project_dir, plan)
    return {"plan": str(plan_path(project_dir)), "added": added, "refreshed": refreshed,
            "unplaced": unplaced, "figures": len(plan["figures"])}


def _sort(plan: dict, book: list) -> None:
    order = {c: i for i, (c, _) in enumerate(book)}
    plan["figures"].sort(key=lambda f: (order.get(f.get("chapter_id"), 10**6),
                                        f.get("leaf") or 0, f["id"]))


def set_fields(project_dir: Path, edits: list[dict]) -> dict:
    """Change figures' editable fields: captions_es, alt_es, alt_en,
    captions_en, chapter_id, chunk_id, anchor_en, status (draft / approved).
    An inserted figure is changed only by removing it first."""
    editable = {"captions_es", "alt_es", "alt_en", "captions_en", "chapter_id", "chunk_id",
                "anchor_en", "status"}
    plan = load_plan(project_dir)
    have = _by_id(plan)
    changed, refused = [], []
    for e in edits:
        fig = have.get(e.get("id"))
        if not fig:
            refused.append({"id": e.get("id"), "why": "no such figure"})
            continue
        if fig.get("status") == "inserted":
            refused.append({"id": fig["id"], "why": "inserted: remove it first"})
            continue
        bad = set(e) - editable - {"id"}
        if bad:
            refused.append({"id": fig["id"], "why": f"not editable: {sorted(bad)}"})
            continue
        if e.get("status") not in (None, "draft", "approved"):
            refused.append({"id": fig["id"], "why": "status is draft or approved"})
            continue
        if e.get("status") == "approved":
            caps_es = e.get("captions_es", fig.get("captions_es")) or []
            caps_en = e.get("captions_en", fig.get("captions_en")) or []
            if len(caps_es) != len(caps_en) or not all(c.strip() for c in caps_es):
                refused.append({"id": fig["id"], "why": "needs its Spanish caption(s) first"})
                continue
        if "anchor_en" in e and e["anchor_en"]:
            e = {**e, "anchor_en": _fold(e["anchor_en"])[:ANCHOR_CHARS]}
        fig.update({k: v for k, v in e.items() if k != "id"})
        changed.append(fig["id"])
    save_plan(project_dir, plan)
    return {"changed": changed, "refused": refused}


# ── placing ─────────────────────────────────────────────────────────────────


def _alignment_rows(project_dir: Path, chapter_id: str) -> list[dict]:
    path = Path(project_dir) / "alignments" / f"{chapter_id}.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("alignments", []) if isinstance(data, dict) else []


def _es_positions(translated: str, rows: list[dict]) -> dict[int, int]:
    """Where each row's Spanish sentence ends in the chunk, found in order (a
    short sentence such as "—Sí." occurs many times; reading in order keeps
    each at its own place)."""
    folded_pos: dict[int, int] = {}
    pos = 0
    for r in sorted(rows, key=lambda r: r.get("es_idx", 0)):
        es = (r.get("es") or "").strip()
        if not es:
            continue
        at = translated.find(es, pos)
        if at < 0:
            continue
        folded_pos[r["es_idx"]] = at + len(es)
        pos = at + len(es)
    return folded_pos


def _figure_block(fig: dict, side: str) -> str:
    alt = (fig.get(f"alt_{side}") or "").replace("]", ")").replace(":", " -")
    caps = fig.get(f"captions_{side}") or []
    parts = [f"[IMAGE:{fig['image']}" + (f":{alt}" if alt else "") + "]"]
    parts += ["[CAPTION] " + " ".join(c.split()) for c in caps if c.strip()]
    return "\n\n".join(parts)


def plan_insert(project_dir: Path, fig: dict, captions: bool = True) -> dict:
    """Where one figure goes in its chunk, on both sides, without writing.
    Returns ``{ok, why?, chunk_id, en_at, es_at, en_context, es_context}``."""
    project_dir = Path(project_dir)
    fid = fig["id"]
    def no(why: str) -> dict:
        return {"ok": False, "id": fid, "why": why}
    if fig.get("status") == "inserted":
        return no("already inserted")
    if not fig.get("anchor_en") or not fig.get("chapter_id"):
        return no("no place yet (chapter_id and anchor_en)")
    caps_en, caps_es = fig.get("captions_en") or [], fig.get("captions_es") or []
    if captions and (len(caps_es) != len(caps_en) or not all(c.strip() for c in caps_es)):
        return no(f"captions_es needs {len(caps_en)} caption(s), one per English caption")
    if not Path(fig.get("src") or "").exists():
        return no(f"picture not found: {fig.get('src')}")
    chunks = [p for p in _chunk_paths(project_dir)
              if p.stem.rsplit("_chunk_", 1)[0] == fig["chapter_id"]]
    hits = []
    for p in chunks:
        c = _chunk_json(p)
        for b in find_anchor(c.get("source_text") or "", fig["anchor_en"]):
            hits.append((p, c, b))
    if len(hits) != 1:
        return no(f"anchor_en opens {len(hits)} paragraphs in {fig['chapter_id']}; "
                  "it must open exactly one")
    path, chunk, (en_start, en_end, en_block) = hits[0]
    meta = chunk.get("metadata") or {}
    src, tgt = chunk.get("source_text") or "", chunk.get("translated_text") or ""
    if not tgt.strip():
        return no(f"{path.stem} is not translated")
    ov_s, ov_e = meta.get("overlap_start") or 0, meta.get("overlap_end") or 0
    if en_end < ov_s or (ov_e and en_start >= len(src) - ov_e):
        return no("the paragraph is in the overlap with the next or previous chunk; "
                  "anchor on another paragraph")
    token = f"[IMAGE:{fig['image']}"
    if token in src or token in tgt:
        return no("the chunk already holds this image")

    # The Spanish side: the last Spanish sentence aligned to the English paragraph.
    rows = [r for r in _alignment_rows(project_dir, fig["chapter_id"])
            if r.get("chunk_id") == path.stem]
    para = _fold(en_block)
    mine = [r for r in rows if (r.get("en") or "").strip() and _fold(r["en"]) in para
            and r.get("es")]
    if not mine:
        return no("no aligned Spanish for the anchor paragraph; run `harness align` "
                  "for the chapter, or anchor on another paragraph")
    pos = _es_positions(tgt, rows)
    ends = [pos[r["es_idx"]] for r in mine if r["es_idx"] in pos]
    if not ends:
        return no("the aligned Spanish is not in the chunk's text (the alignment is "
                  "stale); run `harness align` for the chapter")
    last = max(ends)
    es_block = next((b for b in blocks(tgt) if b[0] < last <= b[1]), None)
    if es_block is None:
        return no("could not find the Spanish paragraph")
    return {
        "ok": True, "id": fid, "chunk_id": path.stem, "chapter_id": fig["chapter_id"],
        "en_at": en_end, "es_at": es_block[1],
        "en_context": en_block, "es_context": es_block[2],
        "en_insert": _figure_block(fig, "en"), "es_insert": _figure_block(fig, "es"),
    }


def _insert(text: str, at: int, block: str) -> str:
    return text[:at] + "\n\n" + block + text[at:]


def _source_insert(source: str, anchor: str, block: str) -> tuple[str, Optional[str]]:
    hits = find_anchor(source, anchor)
    if len(hits) != 1:
        return source, f"anchor opens {len(hits)} paragraphs in source.txt"
    return _insert(source, hits[0][1], block), None


def _strip_figure(text: str, image: str) -> str:
    """The text without the image's block and the caption blocks right under it."""
    bs = blocks(text)
    for i, (_, _, blk) in enumerate(bs):
        m = _IMAGE_RE.match(blk)
        if not (m and m.group(1) == image):
            continue
        j = i + 1
        while j < len(bs) and bs[j][2].startswith("[CAPTION]"):
            j += 1
        if i:  # from the end of the block before to the end of the last caption
            return text[:bs[i - 1][1]] + text[bs[j - 1][1]:]
        return text[bs[j][0]:] if j < len(bs) else ""
    return text


# ── apply / remove ──────────────────────────────────────────────────────────


def _pending_on(project_dir: Path, chapters: set[str]) -> list[dict]:
    from src.harness.text_edit import _pending_rows
    return [r for r in _pending_rows(project_dir) if r.get("chapter_id") in chapters]


def _after_write(project_dir: Path, chunk_ids: set[str], reason: str) -> dict:
    from src.harness.text_edit import _quiet_stdout, _realign
    from src.utils.file_io import load_chunk
    from web_ui.evaluations import evaluate_and_persist_chunk, mark_evaluation_stale

    for cid in sorted(chunk_ids):
        mark_evaluation_stale(project_dir, cid, reason)
    chapters = sorted({c.rsplit("_chunk_", 1)[0] for c in chunk_ids})
    realigned, errors = _realign(project_dir, chapters)
    eval_errors = []
    for cid in sorted(chunk_ids):
        try:
            with _quiet_stdout():
                evaluate_and_persist_chunk(project_dir,
                                           load_chunk(project_dir / "chunks" / f"{cid}.json"))
        except Exception as exc:  # noqa: BLE001 - the text landed; report the evaluator
            eval_errors.append({"chunk_id": cid, "error": str(exc)})
    return {"chapters_realigned": realigned, "realign_errors": errors,
            "evaluation_errors": eval_errors}


def apply(project_dir: Path, ids: Optional[list[str]] = None, dry_run: bool = False) -> dict:
    """Insert the approved figures (or the ones named). All are planned first;
    if one cannot be placed, nothing is written."""
    from src.quotations.apply import _write_chunk_snapshot
    from src.utils.file_io import load_chunk, save_chunk

    project_dir = Path(project_dir)
    plan = load_plan(project_dir)
    figs = [f for f in plan.get("figures", [])
            if (f["id"] in ids if ids else f.get("status") == "approved")]
    if ids:
        missing = set(ids) - {f["id"] for f in figs}
        if missing:
            return {"status": "error", "error": f"no such figure(s): {sorted(missing)}"}
    if not figs:
        return {"status": "ok", "dry_run": dry_run, "planned": [], "refused": [],
                "instructions": "Nothing to insert: approve figures first (board or `figures set`)."}
    planned = [plan_insert(project_dir, f) for f in figs]
    refused = [p for p in planned if not p["ok"]]
    ok = [p for p in planned if p["ok"]]
    if refused or dry_run:
        return {"status": "error" if refused and not dry_run else "ok", "dry_run": dry_run,
                "write": False, "planned": ok, "refused": refused,
                "instructions": ("Fix the refused figures and run again; nothing was written."
                                 if refused else "Run again without --dry-run to insert.")}
    chapters = {p["chapter_id"] for p in ok}
    pending = _pending_on(project_dir, chapters)
    if pending:
        return {"status": "error", "write": False,
                "error": "the reader has unapplied Saves in these chapters, which the realign "
                         "would hide; press Apply corrections in the reader first",
                "chapters": sorted({r.get('chapter_id') for r in pending})}

    source_path = project_dir / "source.txt"
    source = source_path.read_text(encoding="utf-8") if source_path.exists() else None
    fig_by = _by_id(plan)
    # Within a chunk, insert from the end so earlier offsets stay good.
    by_chunk: dict[str, list[dict]] = {}
    for p in ok:
        by_chunk.setdefault(p["chunk_id"], []).append(p)
    if source is not None:
        for p in ok:
            source, err = _source_insert(source, fig_by[p["id"]]["anchor_en"], p["en_insert"])
            if err:
                return {"status": "error", "write": False,
                        "error": f"{p['id']}: {err}; nothing was written"}
    ts = time.strftime("%Y%m%dT%H%M%S")
    backups = []
    for cid, ps in by_chunk.items():
        path = project_dir / "chunks" / f"{cid}.json"
        backups.append(str(_write_chunk_snapshot(project_dir, cid.rsplit("_chunk_", 1)[0],
                                                 cid, path, ts)))
        chunk = load_chunk(path)
        src, tgt = chunk.source_text, chunk.translated_text
        for p in sorted(ps, key=lambda p: p["en_at"], reverse=True):
            src = _insert(src, p["en_at"], p["en_insert"])
        for p in sorted(ps, key=lambda p: p["es_at"], reverse=True):
            tgt = _insert(tgt, p["es_at"], p["es_insert"])
        chunk.source_text, chunk.translated_text = src, tgt
        save_chunk(chunk, path)
    if source is not None:
        source_path.write_text(source, encoding="utf-8")
    (project_dir / "images").mkdir(exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    for p in ok:
        fig = fig_by[p["id"]]
        shutil.copyfile(fig["src"], project_dir / fig["image"])
        fig.update({"status": "inserted", "inserted_at": stamp, "chunk_id": p["chunk_id"]})
    save_plan(project_dir, plan)
    after = _after_write(project_dir, set(by_chunk), "figures inserted by harness figures apply")
    return {"status": "ok" if not after["realign_errors"] else "partial", "dry_run": False,
            "write": True, "inserted": [p["id"] for p in ok], "backups": backups, **after,
            "instructions": "Inserted. Rebuild the EPUB (`harness epub`) to see them there."}


def remove(project_dir: Path, ids: list[str], dry_run: bool = False) -> dict:
    """Take inserted figures out of the source, the chunks and images/."""
    from src.quotations.apply import _write_chunk_snapshot
    from src.utils.file_io import load_chunk, save_chunk

    project_dir = Path(project_dir)
    plan = load_plan(project_dir)
    have = _by_id(plan)
    figs = [have[i] for i in ids if i in have]
    if len(figs) != len(ids):
        return {"status": "error", "error": f"no such figure(s): {sorted(set(ids) - set(have))}"}
    touched: set[str] = set()
    found = {}
    for path in _chunk_paths(project_dir):
        c = _chunk_json(path)
        for f in figs:
            token = f"[IMAGE:{f['image']}"
            if token in (c.get("source_text") or "") or token in (c.get("translated_text") or ""):
                found.setdefault(f["id"], []).append(path.stem)
                touched.add(path.stem)
    if dry_run:
        return {"status": "ok", "dry_run": True, "found": found}
    chapters = {c.rsplit("_chunk_", 1)[0] for c in touched}
    pending = _pending_on(project_dir, chapters)
    if pending:
        return {"status": "error", "error": "the reader has unapplied Saves in these chapters; "
                "press Apply corrections in the reader first"}
    ts = time.strftime("%Y%m%dT%H%M%S")
    for cid in sorted(touched):
        path = project_dir / "chunks" / f"{cid}.json"
        _write_chunk_snapshot(project_dir, cid.rsplit("_chunk_", 1)[0], cid, path, ts)
        chunk = load_chunk(path)
        for f in figs:
            chunk.source_text = _strip_figure(chunk.source_text or "", f["image"])
            chunk.translated_text = _strip_figure(chunk.translated_text or "", f["image"])
        save_chunk(chunk, path)
    source_path = project_dir / "source.txt"
    if source_path.exists():
        s = source_path.read_text(encoding="utf-8")
        for f in figs:
            s = _strip_figure(s, f["image"])
        source_path.write_text(s, encoding="utf-8")
    for f in figs:
        img = project_dir / f["image"]
        if img.exists():
            img.unlink()
        f["status"] = "draft"
        f.pop("inserted_at", None)
    save_plan(project_dir, plan)
    after = _after_write(project_dir, touched, "figures removed by harness figures remove") \
        if touched else {}
    return {"status": "ok", "removed": [f["id"] for f in figs], "chunks": sorted(touched), **after}


def status(project_dir: Path) -> dict:
    plan = load_plan(project_dir)
    figs = plan.get("figures", [])
    counts: dict[str, int] = {}
    for f in figs:
        counts[f.get("status", "draft")] = counts.get(f.get("status", "draft"), 0) + 1
    return {"plan": str(plan_path(project_dir)), "source": plan.get("source"), "counts": counts,
            "figures": [{k: f.get(k) for k in ("id", "status", "chapter_id", "subject",
                                                 "captions_en", "captions_es", "anchor_en")}
                        for f in figs]}


# ── the board (/figures/<id>) ───────────────────────────────────────────────


def chapter_list(project_dir: Path) -> list[dict]:
    """``[{chapter_id, heading}]`` in book order: the heading is the chunk's first block."""
    out = []
    for chapter, chunks in chapters_en(project_dir):
        first = blocks(chunks[0][1]) if chunks else []
        out.append({"chapter_id": chapter, "heading": first[0][2][:90] if first else chapter})
    return out


def paragraphs(project_dir: Path, chapter_id: str) -> list[dict]:
    """The chapter's English paragraphs a figure can follow: ``[{anchor, text}]``
    (images, captions and the heading left out; an anchor that would open more
    than one paragraph is left out too, since it could not be placed)."""
    out = []
    for chapter, chunks in chapters_en(project_dir):
        if chapter != chapter_id:
            continue
        for chunk_id, text in chunks:
            bs = blocks(text)
            seen: dict[str, int] = {}
            for _, _, blk in bs:
                seen[_anchor_of(blk)] = seen.get(_anchor_of(blk), 0) + 1
            for k, (_, _, blk) in enumerate(bs):
                a = _anchor_of(blk)
                if k == 0 or blk.startswith("[") or seen[a] > 1:
                    continue
                out.append({"anchor": a, "chunk_id": chunk_id, "text": blk[:400]})
    return out


def board_state(project_dir: Path) -> dict:
    """Every figure with the place it would land on each side (a dry run of
    :func:`plan_insert`, or why it cannot be placed yet)."""
    project_dir = Path(project_dir)
    plan = load_plan(project_dir)
    rows = []
    for f in plan.get("figures", []):
        row = {k: f.get(k) for k in ("id", "image", "captions_en", "captions_es", "alt_en",
                                      "alt_es", "chapter_id", "anchor_en", "subject", "status",
                                      "doubtful_en", "leaf", "facing_page", "inserted_at")}
        if f.get("status") != "inserted":
            p = plan_insert(project_dir, f, captions=False)
            if p["ok"]:
                row["preview"] = {k: p[k] for k in ("en_context", "es_context")}
            else:
                row["problem"] = p["why"]
            caps_es = f.get("captions_es") or []
            if len(caps_es) != len(f.get("captions_en") or []) or not all(c.strip() for c in caps_es):
                row["missing_es"] = True
        rows.append(row)
    return {"figures": rows, "chapters": chapter_list(project_dir), "source": plan.get("source")}


def picture_path(project_dir: Path, fid: str) -> Optional[Path]:
    """The figure's picture: the project's copy once inserted, else where it came from."""
    fig = _by_id(load_plan(project_dir)).get(fid)
    if not fig:
        return None
    for p in (Path(project_dir) / fig["image"], Path(fig.get("src") or "")):
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            return p
    return None
