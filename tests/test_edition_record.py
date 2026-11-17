"""Tests for src.edition_record: the notes, the hand edits and the source diff."""

import json
import zipfile
from pathlib import Path

from src import edition_record


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "book"
    project.mkdir()
    return project


def _corrections(project: Path, rows: list[dict]) -> None:
    (project / "corrections_applied.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


# ── notes ──────────────────────────────────────────────────────────────────

def test_notes_groups_by_chapter_and_names_what_looks_wrong(tmp_path):
    project = _project(tmp_path)
    page = ('<h2>Capítulo I</h2>'
            '<p class="endnote"><a id="en-1" href="c1.xhtml#enref-1">1.</a> ' + "Una nota larga. " * 12
            + '</p><p class="endnote"><a id="en-2" href="c1.xhtml#enref-2">2.</a> citation?</p>'
            '<h2>Capítulo IV</h2>'
            '<p class="endnote"><a id="en-3" href="c4.xhtml#enref-3">3.</a> Roma</p>')
    with zipfile.ZipFile(project / "book.epub", "w") as zf:
        zf.writestr("EPUB/endnotes.xhtml", f"<html><body>{page}</body></html>")

    result = edition_record.notes(project)

    assert result["ok"] and result["count"] == 3 and result["chapters_with_notes"] == 2
    assert [c["chapter"] for c in result["by_chapter"]] == ["Capítulo I", "Capítulo IV"]
    first = result["by_chapter"][0]["notes"][0]
    assert first["number"] == 1 and len(first["text"]) <= edition_record.NOTE_EXCERPT
    assert first["text"].endswith("…") and "looks_like" not in first
    assert result["memo_like"] == [2] and result["short_unpunctuated"] == [3]
    assert result["by_chapter"][1]["notes"][0]["looks_like"] == "short"


def test_notes_without_a_build(tmp_path):
    result = edition_record.notes(_project(tmp_path))
    assert result["ok"] is False and "no built EPUB" in result["error"]


# ── changes ────────────────────────────────────────────────────────────────

def test_changes_lists_numbers_and_names_and_counts_the_rest(tmp_path):
    project = _project(tmp_path)
    _corrections(project, [
        {"chapter_id": "chapter_03", "es_idx": 9, "en_reference": "Mr. Smith had three sons.",
         "original_es": "El señor Smith tenía tres hijos.",
         "corrected_es": "El señor Díaz tenía tres hijos."},
        {"chapter_id": "chapter_02", "es_idx": 4, "source": "judge:editorial",
         "original_es": "Murió en 1848, según Proverbios 1:10.",
         "corrected_es": "Murió en 1849, según Proverbios 1:7."},
        {"chapter_id": "chapter_02", "es_idx": 1,
         "original_es": "Entonces él se rió mucho.", "corrected_es": "Entonces él se rio mucho."},
        {"chapter_id": "chapter_02", "es_idx": 2, "source": "quotation-pass",
         "original_es": "«Nadie ha visto a Dios.»", "corrected_es": "«Nadie ha visto jamás a Dios.» (Jn 1:18)"},
        {"chapter_id": "chapter_05", "es_idx": 3, "status": "skipped",
         "original_es": "Roma", "corrected_es": "París"},
    ])

    result = edition_record.changes(project)

    assert result["total"] == 5 and result["listed"] == 2
    assert result["left_out"] == {"style only": 1, "quotation-pass": 1, "skipped": 1}
    numbers, names = result["rows"]  # book order: chapter_02 before chapter_03
    assert numbers["chapter"] == "chapter_02" and numbers["by"] == "judge"
    assert numbers["changed"] == ["numbers"]
    assert numbers["removed"] == ["1848", "1:10"] and numbers["added"] == ["1849", "1:7"]
    assert names["changed"] == ["names"] and names["by"] == "reader"
    assert names["removed"] == ["Smith"] and names["added"] == ["Díaz"]
    assert names["english"] == "Mr. Smith had three sons."


def test_changes_does_not_take_a_sentence_opening_for_a_name(tmp_path):
    project = _project(tmp_path)
    _corrections(project, [
        {"chapter_id": "chapter_01", "es_idx": 1,
         "original_es": "Luego vino el frío. Todos lo sintieron.",
         "corrected_es": "Después vino el frío. Cada uno lo sintió."},
        {"chapter_id": "chapter_01", "es_idx": 2,
         "original_es": "—Vamos —dijo. «Nadie lo sabe.»",
         "corrected_es": "—Venga —dijo. «Ninguno lo sabe.»"},
        # A capital of reverence, and a word that only moved to open a quotation.
        {"chapter_id": "chapter_01", "es_idx": 3,
         "original_es": "Espero en él, y en su palabra; venga tu reino.",
         "corrected_es": "Espero en Él, y en Su palabra; venga Tu reino."},
        {"chapter_id": "chapter_01", "es_idx": 4,
         "original_es": "Dijo que _es necesario creer_ y ¡oh, qué gozo!",
         "corrected_es": "Dijo: _Porque es necesario creer_ y ¡Oh, qué gozo!"},
    ])
    result = edition_record.changes(project)
    assert result["listed"] == 0 and result["left_out"] == {"style only": 4}


def test_changes_without_a_log(tmp_path):
    result = edition_record.changes(_project(tmp_path))
    assert result["ok"] and result["rows"] == [] and "no hand edit" in result["note"]


# ── source diff ────────────────────────────────────────────────────────────

REVIEWED = """SERMON XIII.

Good children like to see taxes paid. It is very wicked not to pay them,
and it is very wicked to cheat. "Render to Caesar the things that are Caesar's."

The end of the sermon.
"""

EDITED = """SERMON XIII.

Good children like to see debts paid. "Render to Cæsar
the things that are Caesar's" (Matt. 22:21).

The End of the Sermon
"""


def test_source_diff_reports_words_and_ignores_case_punctuation_and_wrapping(tmp_path):
    project = _project(tmp_path)
    (project / "source.txt").write_text(EDITED, encoding="utf-8")
    reviewed = tmp_path / "reviewed.txt"
    reviewed.write_text(REVIEWED, encoding="utf-8")

    result = edition_record.source_diff(project, reviewed)

    assert result["ok"] and result["hunk_count"] == len(result["hunks"]) and not result["truncated"]
    removed_text = " ".join(h["removed"] for h in result["hunks"])
    added_text = " ".join(h["added"] for h in result["hunks"])
    assert "taxes" in removed_text and "debts" in added_text
    assert "It is very wicked not to pay them" in removed_text
    assert "Matt 22 21" in added_text
    assert result["by_size"]["10+ words"] == 1 and sum(result["by_size"].values()) == result["hunk_count"]
    # Asking for the rewrites only leaves the one-word fixes out of the list, not the count.
    large = edition_record.source_diff(project, reviewed, min_words=10)
    assert len(large["hunks"]) == 1 and large["hunk_count"] == result["hunk_count"]
    assert "wicked" in large["hunks"][0]["removed"] and not large["truncated"]
    # "The end of the sermon." against "The End of the Sermon": case and the period only,
    # and the re-wrapped quotation, are not differences.
    assert not any("sermon" in (h["removed"] + h["added"]).lower() for h in result["hunks"])
    assert "Render" not in removed_text and "Render" not in added_text
    assert {h["kind"] for h in result["hunks"]} <= {"removed", "added", "changed"}
    first = result["hunks"][0]
    assert first["reviewed_line"] == 3 and first["source_line"] == 3
    assert first["context"].endswith("to see")


def test_source_diff_of_the_same_words_is_empty(tmp_path):
    project = _project(tmp_path)
    (project / "source.txt").write_text("One  two,\nthree. FOUR!", encoding="utf-8")
    same = tmp_path / "same.txt"
    same.write_text("one two three\n\nfour", encoding="utf-8")
    result = edition_record.source_diff(project, same)
    assert result["hunks"] == [] and result["removed_words"] == 0 and result["added_words"] == 0


def test_source_diff_caps_the_list_and_keeps_the_count(tmp_path):
    project = _project(tmp_path)
    (project / "source.txt").write_text(
        " ".join(f"same{n} new{n}" for n in range(12)), encoding="utf-8")
    old = tmp_path / "old.txt"
    old.write_text(" ".join(f"same{n} old{n}" for n in range(12)), encoding="utf-8")
    result = edition_record.source_diff(project, old, max_hunks=5)
    assert len(result["hunks"]) == 5 and result["hunk_count"] == 12 and result["truncated"]
    assert result["removed_words"] == 12 and result["added_words"] == 12


def test_source_diff_finds_an_inserted_sentence_among_repeated_ones(tmp_path):
    # Sentences are matched first: a sentence inserted mid-book is one hunk, a
    # repeated sentence («—dijo él.») does not shift what follows, and moving a
    # sentence boundary («is. He» → «is, he») is not a difference.
    paragraphs = [f"Paragraph {n} opens here. —dijo él. It closes with word{n}." for n in range(40)]
    old_text = "\n\n".join(paragraphs).replace("opens here. —dijo", "opens here, —dijo", 1)
    new = list(paragraphs)
    new.insert(20, "A brand new sentence arrives.")
    project = _project(tmp_path)
    (project / "source.txt").write_text("\n\n".join(new), encoding="utf-8")
    reviewed = tmp_path / "reviewed.txt"
    reviewed.write_text(old_text, encoding="utf-8")

    result = edition_record.source_diff(project, reviewed)

    assert result["hunk_count"] == 1 and result["removed_words"] == 0
    hunk = result["hunks"][0]
    assert hunk["kind"] == "added" and hunk["added"] == "A brand new sentence arrives"
    assert hunk["source_line"] == 41


def test_source_diff_names_a_missing_file(tmp_path):
    project = _project(tmp_path)
    result = edition_record.source_diff(project, tmp_path / "nowhere.txt")
    assert result["ok"] is False and "source.txt" in result["error"]
