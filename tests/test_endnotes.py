"""Tests for the endnotes module and its EPUB integration."""

import json
import re
import zipfile
from pathlib import Path

from src.endnotes import (
    Endnote,
    build_endnote_artifacts,
    parse_endnote_content,
    render_endnotes_xhtml,
)
from src.epub_builder import build_epub


# --- helpers ---

def _write_annotations(project: Path, records):
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    (project / "annotations.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_alignment(project: Path, chapter_id: str, es_by_idx: dict):
    aln_dir = project / "alignments"
    aln_dir.mkdir(exist_ok=True)
    payload = {
        "chapter_id": chapter_id,
        "alignments": [
            {"es_idx": idx, "es": es} for idx, es in sorted(es_by_idx.items())
        ],
    }
    (aln_dir / f"{chapter_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# --- parse_endnote_content ---

class TestParseEndnoteContent:
    def test_leading_bracket(self):
        assert parse_endnote_content("[Sancerre] Pueblo del Loira.") == (
            "Sancerre", "Pueblo del Loira.")

    def test_trailing_bracket(self):
        # Legacy fabre2-style note: bracket at the end.
        assert parse_endnote_content("Tb. cambray [batista,]") == (
            "batista,", "Tb. cambray")

    def test_punctuation_inside_bracket(self):
        anchor, text = parse_endnote_content("[entonces,] nota aquí")
        assert anchor == "entonces,"
        assert text == "nota aquí"

    def test_no_bracket(self):
        assert parse_endnote_content("solo texto") == (None, "solo texto")

    def test_bracket_only_is_empty_text(self):
        # Bare placeholder -> empty display text (will be skipped downstream).
        assert parse_endnote_content("[Sancerre]") == ("Sancerre", "")

    def test_empty_brackets_treated_as_no_anchor(self):
        assert parse_endnote_content("[] texto") == (None, "texto")

    def test_whitespace_collapsed(self):
        anchor, text = parse_endnote_content("uno\n\n  dos  [x]  tres")
        assert anchor == "x"
        assert text == "uno dos tres"

    def test_only_first_bracket_is_anchor(self):
        anchor, text = parse_endnote_content("[a] mid [b] end")
        assert anchor == "a"
        assert text == "mid [b] end"


# --- build_endnote_artifacts ---

class TestBuildEndnoteArtifacts:
    def test_global_numbering_across_chapters(self, tmp_path):
        _write_annotations(tmp_path, [
            {"chapter_id": "chapter_01", "es_idx": 2, "type": "footnote",
             "content": "[Sancerre] Pueblo del Loira."},
            {"chapter_id": "chapter_02", "es_idx": 1, "type": "footnote",
             "content": "Una nota cualquiera."},
        ])
        _write_alignment(tmp_path, "chapter_01", {
            2: "Había un castaño de Sancerre muy grande."})
        _write_alignment(tmp_path, "chapter_02", {
            1: "Otra frase importante."})

        ordered = [
            ("chapter_01", "Había un castaño de Sancerre muy grande."),
            ("chapter_02", "Otra frase importante."),
        ]
        injected, entries = build_endnote_artifacts(tmp_path, ordered)

        assert [e.number for e in entries] == [1, 2]
        assert entries[0].chapter_id == "chapter_01"
        assert entries[1].chapter_id == "chapter_02"
        # Marker lands right after the anchor (incl. nothing extra).
        assert injected["chapter_01"] == (
            "Había un castaño de Sancerre{{ENDNOTE:1}} muy grande.")
        # No anchor -> end of sentence.
        assert injected["chapter_02"] == "Otra frase importante.{{ENDNOTE:2}}"

    def test_anchor_includes_trailing_punctuation(self, tmp_path):
        _write_annotations(tmp_path, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote",
             "content": "[entonces,] explicación"},
        ])
        _write_alignment(tmp_path, "chapter_01", {
            0: "Y por entonces, todo cambió."})
        injected, entries = build_endnote_artifacts(
            tmp_path, [("chapter_01", "Y por entonces, todo cambió.")])
        assert injected["chapter_01"] == "Y por entonces,{{ENDNOTE:1}} todo cambió."
        assert entries[0].text == "explicación"

    def test_anchor_not_found_falls_back_to_end(self, tmp_path):
        _write_annotations(tmp_path, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote",
             "content": "[noexiste] nota"},
        ])
        _write_alignment(tmp_path, "chapter_01", {0: "Frase sin esa palabra."})
        injected, _ = build_endnote_artifacts(
            tmp_path, [("chapter_01", "Frase sin esa palabra.")])
        assert injected["chapter_01"] == "Frase sin esa palabra.{{ENDNOTE:1}}"

    def test_empty_text_placeholder_skipped(self, tmp_path):
        _write_annotations(tmp_path, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote",
             "content": "[Sancerre]"},
        ])
        _write_alignment(tmp_path, "chapter_01", {0: "Algo sobre Sancerre aquí."})
        injected, entries = build_endnote_artifacts(
            tmp_path, [("chapter_01", "Algo sobre Sancerre aquí.")])
        assert entries == []
        assert injected["chapter_01"] == "Algo sobre Sancerre aquí."

    def test_sentence_not_found_skipped(self, tmp_path):
        _write_annotations(tmp_path, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote",
             "content": "nota"},
        ])
        _write_alignment(tmp_path, "chapter_01", {0: "Esta frase no está en el cuerpo."})
        injected, entries = build_endnote_artifacts(
            tmp_path, [("chapter_01", "Un cuerpo totalmente distinto.")])
        assert entries == []
        assert injected["chapter_01"] == "Un cuerpo totalmente distinto."

    def test_removed_annotation_excluded(self, tmp_path):
        _write_annotations(tmp_path, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote",
             "content": "nota"},
            {"chapter_id": "chapter_01", "es_idx": 0, "removed": True},
        ])
        _write_alignment(tmp_path, "chapter_01", {0: "Frase de prueba."})
        _, entries = build_endnote_artifacts(
            tmp_path, [("chapter_01", "Frase de prueba.")])
        assert entries == []

    def test_later_non_footnote_supersedes(self, tmp_path):
        # A later word_choice at the same es_idx replaces the footnote.
        _write_annotations(tmp_path, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote",
             "content": "nota cultural"},
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice",
             "content": "otra cosa"},
        ])
        _write_alignment(tmp_path, "chapter_01", {0: "Frase de prueba."})
        _, entries = build_endnote_artifacts(
            tmp_path, [("chapter_01", "Frase de prueba.")])
        assert entries == []

    def test_non_footnote_types_ignored(self, tmp_path):
        _write_annotations(tmp_path, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice",
             "content": "mejor palabra"},
        ])
        _write_alignment(tmp_path, "chapter_01", {0: "Frase de prueba."})
        injected, entries = build_endnote_artifacts(
            tmp_path, [("chapter_01", "Frase de prueba.")])
        assert entries == []
        assert injected["chapter_01"] == "Frase de prueba."

    def test_no_annotations_file_is_noop(self, tmp_path):
        injected, entries = build_endnote_artifacts(
            tmp_path, [("chapter_01", "Texto sin notas.")])
        assert entries == []
        assert injected == {"chapter_01": "Texto sin notas."}


# --- render_endnotes_xhtml ---

class TestRenderEndnotesXhtml:
    def test_groups_by_chapter_with_links(self):
        entries = [
            Endnote("chapter_01", 2, 1, "Pueblo del Loira."),
            Endnote("chapter_01", 5, 2, "Otra nota."),
            Endnote("chapter_02", 1, 3, "Tercera nota."),
        ]
        headings = {"chapter_01": "Capítulo 1", "chapter_02": "Capítulo 2"}
        files = {"chapter_01": "chapter_01.xhtml", "chapter_02": "chapter_02.xhtml"}
        html = render_endnotes_xhtml(entries, headings, files)

        assert "<h1>Notas</h1>" in html
        assert "<h2>Capítulo 1</h2>" in html
        assert "<h2>Capítulo 2</h2>" in html
        # Forward target id + back link to in-text marker.
        assert 'id="en-1"' in html
        assert 'href="chapter_01.xhtml#enref-1"' in html
        assert "Pueblo del Loira." in html
        assert "Tercera nota." in html
        # Chapter 1 group comes before chapter 2.
        assert html.index("Capítulo 1") < html.index("Capítulo 2")

    def test_escapes_text(self):
        entries = [Endnote("chapter_01", 0, 1, "a < b & c")]
        html = render_endnotes_xhtml(
            entries, {"chapter_01": "C"}, {"chapter_01": "chapter_01.xhtml"})
        assert "a &lt; b &amp; c" in html


# --- end-to-end EPUB integration ---

class TestEndnotesEpubIntegration:
    def _make_project(self, tmp_path):
        chapters_dir = tmp_path / "chapters"
        chapters_dir.mkdir()
        (tmp_path / "images").mkdir()
        (chapters_dir / "chapter_01.txt").write_text(
            "CAPÍTULO I\n\nEl Inicio\n\n"
            "Había un castaño de Sancerre muy grande. Y luego pasó algo más.",
            encoding="utf-8",
        )
        _write_alignment(tmp_path, "chapter_01", {
            0: "CAPÍTULO I",
            1: "El Inicio",
            2: "Había un castaño de Sancerre muy grande.",
            3: "Y luego pasó algo más.",
        })
        return tmp_path

    def test_markers_section_and_ordering(self, tmp_path):
        project = self._make_project(tmp_path)
        _write_annotations(project, [
            {"chapter_id": "chapter_01", "es_idx": 2, "type": "footnote",
             "content": "[Sancerre] Pueblo del valle del Loira."},
        ])

        output = build_epub(
            project_path=project,
            title="Libro",
            author="Autor",
            language="es",
            translator_note_body="Una nota final del traductor.",
        )
        assert output.exists()

        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
            assert any(n.endswith("endnotes.xhtml") for n in names)

            chapter_name = next(n for n in names if n.endswith("chapter_01.xhtml"))
            chapter_html = zf.read(chapter_name).decode("utf-8")
            # Superscript reference injected right after the anchor word.
            assert 'Sancerre<sup class="endnote-ref">' in chapter_html
            assert 'href="endnotes.xhtml#en-1"' in chapter_html

            endnotes_name = next(n for n in names if n.endswith("endnotes.xhtml"))
            endnotes_html = zf.read(endnotes_name).decode("utf-8")
            assert "<h1>Notas</h1>" in endnotes_html
            assert "<h2>Capítulo I</h2>" in endnotes_html
            assert "Pueblo del valle del Loira." in endnotes_html
            assert 'id="en-1"' in endnotes_html

            # Spine order: chapter -> endnotes -> translator note.
            opf_name = next(n for n in names if n.endswith(".opf"))
            opf = zf.read(opf_name).decode("utf-8")
            spine = re.search(r"<spine.*?</spine>", opf, re.DOTALL).group(0)
            idrefs = re.findall(r'idref="([^"]+)"', spine)
            assert "endnotes" in idrefs
            assert "translator_note" in idrefs
            assert idrefs.index("endnotes") < idrefs.index("translator_note")
            # Endnotes is not the very first content item (a chapter precedes it).
            assert idrefs.index("endnotes") > 0

    def test_no_footnotes_means_no_section(self, tmp_path):
        project = self._make_project(tmp_path)
        # Only a non-footnote annotation present.
        _write_annotations(project, [
            {"chapter_id": "chapter_01", "es_idx": 2, "type": "word_choice",
             "content": "mejor palabra"},
        ])
        output = build_epub(
            project_path=project, title="Libro", author="Autor", language="es",
        )
        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
            assert not any(n.endswith("endnotes.xhtml") for n in names)
            chapter_name = next(n for n in names if n.endswith("chapter_01.xhtml"))
            chapter_html = zf.read(chapter_name).decode("utf-8")
            assert "endnote-ref" not in chapter_html
