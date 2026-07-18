#!/usr/bin/env python3
"""
Translate imported Gutenberg footnote bodies — whole book, on demand.

Reads ``footnotes.json`` (written by the ingest ``--footnotes import`` pass),
translates each note's ``source_body`` into the target language using the book's
glossary + style guide for context, and writes the result back into the same
file's ``translated_body``. This is deliberately decoupled from the main body
translation so it can be run whenever the footnotes exist, independent of where
the chapters are in the pipeline.

Usage:
    python scripts/translate_footnotes.py --project-dir projects/mybook
    python scripts/translate_footnotes.py --project-dir projects/mybook --yes
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.footnote_import import load_footnotes_sidecar, footnotes_sidecar_path, write_footnotes_sidecar, FootnoteRecord
from src.utils.file_io import load_glossary, load_style_guide


# Keep each batch's source text under this many characters so the model's reply
# stays manageable and easy to reconcile.
_BATCH_CHAR_BUDGET = 6000

_LINE_RE = re.compile(r"^\s*(\d+)\s*\|\s*(.*)$")


def batch_notes(notes: list[dict], char_budget: int = _BATCH_CHAR_BUDGET) -> list[list[dict]]:
    """Group untranslated notes into batches under a character budget."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for n in notes:
        body = n.get("source_body") or ""
        if current and size + len(body) > char_budget:
            batches.append(current)
            current, size = [], 0
        current.append(n)
        size += len(body)
    if current:
        batches.append(current)
    return batches


def build_footnotes_prompt(
    notes: list[dict],
    *,
    source_language: str,
    target_language: str,
    title: str,
    glossary_text: str = "",
    style_text: str = "",
) -> str:
    """Render the batch translation prompt for a group of footnotes."""
    parts = [
        f'You are a professional literary translator. Translate these footnotes '
        f'from {source_language} to {target_language} for the book "{title}".',
        "",
        "Rules:",
        f"- Translate each numbered note into natural, fluent {target_language}.",
        "- Preserve _underscore italics_ exactly (they mark italic text).",
        "- Preserve any [IMAGE:...] tokens verbatim.",
        "- Do NOT add, drop, merge, or renumber notes.",
        "- Return EXACTLY one line per note, formatted as:  N| <translation>",
        "",
    ]
    if glossary_text.strip():
        parts += ["GLOSSARY (use these renderings):", glossary_text.strip(), ""]
    if style_text.strip():
        parts += ["STYLE GUIDE:", style_text.strip(), ""]
    parts.append("FOOTNOTES:")
    for n in notes:
        parts.append(f"{n['number']}| {n.get('source_body') or ''}")
    return "\n".join(parts)


def parse_numbered_translations(response: str) -> dict[int, str]:
    """Parse ``N| translation`` lines, joining wrapped continuation lines."""
    out: dict[int, str] = {}
    current: int | None = None
    for line in (response or "").splitlines():
        m = _LINE_RE.match(line)
        if m:
            current = int(m.group(1))
            out[current] = m.group(2).strip()
        elif current is not None and line.strip():
            out[current] = (out[current] + " " + line.strip()).strip()
    return out


def _glossary_text(project_dir: Path) -> str:
    path = project_dir / "glossary.json"
    if not path.exists():
        return ""
    try:
        from src.api_translator import format_glossary_for_prompt
        return format_glossary_for_prompt(load_glossary(path))
    except Exception:
        return ""


def _style_text(project_dir: Path) -> str:
    path = project_dir / "style.json"
    if not path.exists():
        return ""
    try:
        return load_style_guide(path).content
    except Exception:
        return ""


def translate_footnotes(
    project_dir: Path,
    *,
    provider: str,
    model: str,
    source_language: str,
    target_language: str,
    title: str,
    retranslate: bool = False,
) -> int:
    """Translate all (untranslated) footnote bodies in place. Returns count done."""
    from src.api_translator import call_llm

    notes = load_footnotes_sidecar(project_dir)
    if not notes:
        print("  No footnotes.json — nothing to translate.")
        return 0

    pending = [n for n in notes if retranslate or not (n.get("translated_body") or "").strip()]
    if not pending:
        print(f"  All {len(notes)} footnotes already translated.")
        return 0

    glossary_text = _glossary_text(project_dir)
    style_text = _style_text(project_dir)
    by_number = {n["number"]: n for n in notes}

    print(f"  Translating {len(pending)} of {len(notes)} footnotes "
          f"({source_language} -> {target_language}) with {provider}/{model} ...")

    done = 0
    for batch in batch_notes(pending):
        prompt = build_footnotes_prompt(
            batch, source_language=source_language, target_language=target_language,
            title=title, glossary_text=glossary_text, style_text=style_text,
        )
        response = call_llm(prompt, provider=provider, model=model,
                            max_tokens=4096, call_type="footnotes")
        translations = parse_numbered_translations(response)

        for n in batch:
            num = n["number"]
            text = translations.get(num, "").strip()
            if not text:
                # Per-note fallback for anything the batch didn't reconcile.
                single = call_llm(
                    build_footnotes_prompt(
                        [n], source_language=source_language, target_language=target_language,
                        title=title, glossary_text=glossary_text, style_text=style_text,
                    ),
                    provider=provider, model=model, max_tokens=1024, call_type="footnotes",
                )
                text = parse_numbered_translations(single).get(num, "").strip()
            if text:
                by_number[num]["translated_body"] = text
                done += 1

    # Persist back to footnotes.json, preserving order.
    records = [FootnoteRecord(
        number=n["number"], ref_marker=n.get("ref_marker", ""),
        source_body=n.get("source_body", ""), detected=n.get("detected", ""),
        translated_body=n.get("translated_body"),
    ) for n in notes]
    write_footnotes_sidecar(project_dir, records)
    print(f"  Translated {done} footnotes -> {footnotes_sidecar_path(project_dir)}")
    return done


def main():
    parser = argparse.ArgumentParser(description="Translate imported Gutenberg footnotes (whole book).")
    parser.add_argument("--project-dir", required=True, help="Project directory containing footnotes.json")
    parser.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"])
    parser.add_argument("--model", default=None, help="Model id (defaults to the translator default)")
    parser.add_argument("--source-lang", default="English")
    parser.add_argument("--target-lang", default="Spanish")
    parser.add_argument("--title", default=None, help="Book title for context (defaults to project dir name)")
    parser.add_argument("--retranslate", action="store_true",
                        help="Re-translate notes that already have a translated_body")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    if not project_dir.exists():
        parser.error(f"project dir not found: {project_dir}")

    from src.api_translator import DEFAULT_MODEL
    translate_footnotes(
        project_dir,
        provider=args.provider,
        model=args.model or DEFAULT_MODEL,
        source_language=args.source_lang,
        target_language=args.target_lang,
        title=args.title or project_dir.name,
        retranslate=args.retranslate,
    )


if __name__ == "__main__":
    main()
