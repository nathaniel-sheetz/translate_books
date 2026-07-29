"""
Write the dated markdown report for one annotation-review run.

The report is the deliverable the reader actually reads before approving
anything, so it has one hard requirement: **it logs each annotation's content as
of run time, verbatim**. Applying a review rewrites (footnotes) or extends (the
other types) that content, and the reader needs the before-state on record.

Reports land in ``projects/<slug>/reports/`` — the directory
``file_io.ensure_project_structure`` already creates for evaluation reports — and
are timestamped so a book accumulates one per run rather than overwriting.

Body text is written in the book's target language, matching the notes the run
appends. Structural labels stay stable so the file is greppable.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Section headings and field labels per target language. A language without an
# entry falls back to English, which keeps the report readable rather than
# half-translated.
_STRINGS = {
    "spanish": {
        "title": "Revisión de anotaciones",
        "project": "Proyecto",
        "generated": "Generado",
        "backend": "Motor",
        "model": "Modelo",
        "scope": "Alcance",
        "whole_book": "libro completo",
        "summary": "Resumen",
        "type": "Tipo",
        "reviewed": "Revisadas",
        "to_append": "Por aplicar",
        "resolved": "Ya resueltas",
        "manual": "Requieren mano",
        "total": "Total",
        "annotation": "Anotación",
        "sentence": "Oración",
        "recommendation": "Recomendación",
        "will_write": "Texto que se escribirá",
        "confidence": "Confianza",
        "evidence": "Base",
        "state_resolved": "Ya contiene una conclusión; no se añadirá nada.",
        "omitted": "Omitidas",
        "reason": "Motivo",
        "failures": "Fallos",
        "no_results": "No se revisó ninguna anotación en esta ejecución.",
        "carried_over": (
            "Este informe cubre solo esta ejecución. `results.json` conserva "
            "además {n} anotación(es) revisada(s) antes, que `apply` todavía "
            "puede escribir; su informe correspondiente está en `reports/`."
        ),
        "mode_replace": "reemplaza el contenido (el texto de una nota al pie se publica)",
        "mode_append": "se añade al final de la nota",
        "types": {
            "word_choice": "Elección de palabra",
            "inconsistency": "Inconsistencia",
            "footnote": "Nota al pie",
            "flag": "Otro",
        },
        "reasons": {
            "imported": "nota importada de Gutenberg; ya trae su texto",
            "already_reviewed": "ya revisada en una ejecución anterior",
            "orphaned": "la oración anclada ya no existe en la alineación",
            "multi_anchor": "la nota marca varios términos; dividirla a mano",
            "no_note_text": "el modelo no propuso texto para añadir",
            "content_changed": "la anotación cambió después de la revisión",
        },
    },
    "english": {
        "title": "Annotation review",
        "project": "Project",
        "generated": "Generated",
        "backend": "Backend",
        "model": "Model",
        "scope": "Scope",
        "whole_book": "whole book",
        "summary": "Summary",
        "type": "Type",
        "reviewed": "Reviewed",
        "to_append": "To apply",
        "resolved": "Already resolved",
        "manual": "Needs a human",
        "total": "Total",
        "annotation": "Annotation",
        "sentence": "Sentence",
        "recommendation": "Recommendation",
        "will_write": "Text that will be written",
        "confidence": "Confidence",
        "evidence": "Basis",
        "state_resolved": "Already carries a conclusion; nothing will be added.",
        "omitted": "Omitted",
        "reason": "Reason",
        "failures": "Failures",
        "no_results": "No annotations were reviewed in this run.",
        "carried_over": (
            "This report covers this run only. `results.json` also still holds "
            "{n} annotation(s) reviewed earlier, which `apply` can still write; "
            "their report is in `reports/`."
        ),
        "mode_replace": "replaces the content (footnote text is published)",
        "mode_append": "appended to the end of the note",
        "types": {
            "word_choice": "Word choice",
            "inconsistency": "Inconsistency",
            "footnote": "Footnote",
            "flag": "Other",
        },
        "reasons": {
            "imported": "imported Gutenberg note; already carries its text",
            "already_reviewed": "already reviewed in an earlier run",
            "orphaned": "the anchored sentence no longer exists in the alignment",
            "multi_anchor": "the note marks several terms; split it by hand",
            "no_note_text": "the model proposed no text to add",
            "content_changed": "the annotation changed after the review",
        },
    },
}

_TYPE_ORDER = ("word_choice", "inconsistency", "footnote", "flag")


def _strings(target_language: Optional[str]) -> dict:
    key = (target_language or "").strip().lower()
    if key in ("spanish", "español", "espanol", "es"):
        return _STRINGS["spanish"]
    return _STRINGS["english"]


def _fence(text: str) -> str:
    """Quote user text as a fenced block so brackets/rayas survive markdown.

    Annotation content is full of ``[...]`` tokens and typographic dashes that
    markdown would otherwise mangle, and the whole point of the report is that
    this text is verbatim.
    """
    body = text if text else ""
    fence = "```"
    while fence in body:
        fence += "`"
    return f"{fence}\n{body}\n{fence}"


def _report_filename(when: datetime) -> str:
    return f"annotations_{when.strftime('%Y%m%d_%H%M%S')}.md"


def render_report(results_doc: dict[str, Any]) -> str:
    """Render the full markdown report for a committed run."""
    s = _strings(results_doc.get("target_language"))
    type_labels = s["types"]
    reasons = s["reasons"]

    results = results_doc.get("results") or []
    skipped = results_doc.get("skipped") or []
    failed = results_doc.get("failed") or []
    missing = results_doc.get("missing") or []

    when = results_doc.get("committed_at") or datetime.now().isoformat()
    chapters = results_doc.get("chapters")
    scope = ", ".join(chapters) if chapters else s["whole_book"]

    lines: list[str] = []
    lines.append(f"# {s['title']} — {results_doc.get('project', '')}")
    lines.append("")
    lines.append(f"- **{s['project']}:** {results_doc.get('project', '')}")
    lines.append(f"- **{s['generated']}:** {when}")
    lines.append(f"- **{s['backend']}:** {results_doc.get('backend', '')}")
    model = results_doc.get("model") or results_doc.get("worker_model")
    if model:
        lines.append(f"- **{s['model']}:** {model}")
    lines.append(f"- **{s['scope']}:** {scope}")
    lines.append("")

    # The apply plan outlives any single run, so a scoped run leaves keys in
    # results.json that this report does not describe. Say so here rather than
    # letting `apply --select` be the place that surprises the reader.
    carried = results_doc.get("carried_over") or 0
    if carried > 0:
        lines.append(f"> {s['carried_over'].format(n=carried)}")
        lines.append("")

    # --- summary table -----------------------------------------------------
    by_type: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_type.setdefault(
            r["type"], {"reviewed": 0, "write": 0, "resolved": 0, "manual": 0}
        )
        bucket["reviewed"] += 1
        if r.get("writable"):
            bucket["write"] += 1
        if r.get("state") == "already_resolved":
            bucket["resolved"] += 1
        if r.get("manual_reason"):
            bucket["manual"] += 1

    lines.append(f"## {s['summary']}")
    lines.append("")
    lines.append(
        f"| {s['type']} | {s['reviewed']} | {s['to_append']} | {s['resolved']} | {s['manual']} |"
    )
    lines.append("|---|---:|---:|---:|---:|")
    totals = {"reviewed": 0, "write": 0, "resolved": 0, "manual": 0}
    for ann_type in _TYPE_ORDER:
        bucket = by_type.get(ann_type)
        if not bucket:
            continue
        for k in totals:
            totals[k] += bucket[k]
        lines.append(
            f"| {type_labels.get(ann_type, ann_type)} | {bucket['reviewed']} "
            f"| {bucket['write']} | {bucket['resolved']} | {bucket['manual']} |"
        )
    lines.append(
        f"| **{s['total']}** | **{totals['reviewed']}** | **{totals['write']}** "
        f"| **{totals['resolved']}** | **{totals['manual']}** |"
    )
    lines.append("")

    if not results:
        lines.append(s["no_results"])
        lines.append("")

    # --- one section per type ---------------------------------------------
    for ann_type in _TYPE_ORDER:
        of_type = [r for r in results if r["type"] == ann_type]
        if not of_type:
            continue
        lines.append(f"## {type_labels.get(ann_type, ann_type)}")
        lines.append("")
        for r in sorted(of_type, key=lambda x: (x["chapter_id"], x["es_idx"])):
            anchors = r.get("anchors") or []
            anchor_bit = f" · `{'`, `'.join(anchors)}`" if anchors else ""
            lines.append(f"### {r['chapter_id']} · {r['es_idx']}{anchor_bit}")
            lines.append("")
            lines.append(f"**{s['annotation']}:**")
            lines.append(_fence(r.get("content") or ""))
            if r.get("es_sentence"):
                lines.append(f"**{s['sentence']}:** {r['es_sentence']}")
                lines.append("")
            if r.get("recommendation"):
                lines.append(f"**{s['recommendation']}:** {r['recommendation']}")
                lines.append("")

            if r.get("state") == "already_resolved":
                lines.append(s["state_resolved"])
                lines.append("")
            elif r.get("writable"):
                mode_note = s["mode_replace"] if r.get("mode") == "replace" else s["mode_append"]
                lines.append(f"**{s['will_write']}** ({mode_note}):")
                lines.append(_fence(r.get("new_content") or ""))
            elif r.get("manual_reason"):
                reason = r["manual_reason"]
                lines.append(
                    f"**{s['manual']}:** {reasons.get(reason, reason)} (`{reason}`)"
                )
                lines.append("")
                # A withheld note still carries drafted text — the whole value of
                # a multi-anchor footnote review is the gloss the reader pastes in.
                if r.get("note_text"):
                    lines.append(_fence(r["note_text"]))

            meta = [f"{s['confidence']}: {r.get('confidence', '')}"]
            if r.get("evidence"):
                meta.append(f"{s['evidence']}: {'; '.join(r['evidence'])}")
            lines.append("<sub>" + " · ".join(meta) + "</sub>")
            lines.append("")

    # --- omitted -----------------------------------------------------------
    if skipped:
        lines.append(f"## {s['omitted']}")
        lines.append("")
        for item in sorted(skipped, key=lambda x: (str(x.get("chapter_id")), x.get("es_idx") or 0)):
            reason = item.get("reason", "")
            label = type_labels.get(item.get("type"), item.get("type", ""))
            lines.append(
                f"- **{item.get('chapter_id')} · {item.get('es_idx')}** ({label}) — "
                f"{reasons.get(reason, reason)} (`{reason}`)"
            )
            content = item.get("content")
            if content:
                lines.append(f"  - `{content}`")
        lines.append("")

    # --- failures ----------------------------------------------------------
    if failed or missing:
        lines.append(f"## {s['failures']}")
        lines.append("")
        for item in failed:
            lines.append(
                f"- `{item.get('key')}` ({item.get('type')}) — {item.get('problem')}"
            )
        for item in missing:
            lines.append(f"- `{item.get('key')}` ({item.get('type')}) — no draft")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_report(project_dir: Path, results_doc: dict[str, Any]) -> Path:
    """Render and write the report; returns the path written."""
    reports_dir = Path(project_dir) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / _report_filename(datetime.now())
    path.write_text(render_report(results_doc), encoding="utf-8")
    return path
