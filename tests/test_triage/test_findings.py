"""Tests for src/triage/findings.py — which findings reach a triage prompt.

The collector decides what a model is asked about. Two failures matter more than
the rest and are pinned here:

* **A finding joined to the wrong sentence.** The verdict would then be about
  prose the checker never flagged, and it would suppress a real defect somewhere
  else. The offset-to-sentence join is the whole reason this module exists.
* **A finding sent when a human already ruled on it.** Re-asking spends tokens to
  overwrite a dismissal, an ignore, or a standing verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.triage import findings as tf
from web_ui.evaluations import append_feedback, append_triage

ES = (
    "Fru Astrida cantaba de los Sigfridos antiguos.\n\n"
    "El niño escuchaba en silencio junto al fuego.\n\n"
    "—Audaz, te lo prometo —dijo el duque.\n"
)

#: One word, three paragraphs — what a checker reports as "(found 3 time(s))".
REPEATED = (
    "Fru Astrida cantaba de los Sigfridos antiguos.\n\n"
    "El niño escuchaba a los Sigfridos en silencio.\n\n"
    "—Los Sigfridos —dijo el duque, y se durmió.\n"
)


def _chunk(project: Path, chunk_id: str, text: str = ES) -> None:
    (project / "chunks").mkdir(parents=True, exist_ok=True)
    (project / "chunks" / f"{chunk_id}.json").write_text(
        json.dumps({"id": chunk_id, "chunk_id": chunk_id, "translated_text": text,
                    "source_text": "Fru Astrida sang of the old Sigfrids."}),
        encoding="utf-8",
    )


def _alignment(project: Path, chapter: str, chunk_id: str, text: str = ES) -> None:
    """One alignment row per paragraph, which is what the splitter yields here."""
    (project / "alignments").mkdir(parents=True, exist_ok=True)
    rows = []
    for idx, para in enumerate(p for p in text.split("\n\n") if p.strip()):
        rows.append({"es_idx": idx, "en_idx": idx, "es": para.strip(),
                     "en": "", "chunk_id": chunk_id})
    (project / "alignments" / f"{chapter}.json").write_text(
        json.dumps({"alignments": rows}), encoding="utf-8",
    )


def _evaluation(project: Path, chunk_id: str, issues: list[dict]) -> None:
    (project / "evaluations").mkdir(parents=True, exist_ok=True)
    (project / "evaluations" / f"{chunk_id}.json").write_text(
        json.dumps({"chunk_id": chunk_id, "normalized_issues": issues}),
        encoding="utf-8",
    )


def _ni(eval_name: str, term: str, text: str = ES, *, index: int = 0,
        rule_id=None, message=None) -> dict:
    """A normalized issue whose char_start really points at ``term`` in the text."""
    start = text.index(term)
    return {
        "eval_name": eval_name,
        "eval_version": "1.0.0",
        "issue_index": index,
        "severity": "warning",
        "message": message or f"'{term}': Unknown word (found 1 time(s))",
        "suggestion": None,
        "location": {
            "raw": f"Character position {start}",
            "side": "target",
            "paragraph_index": 0,
            "char_start": start,
            "char_end": start + len(term),
            "snippet_before": "",
            "match": term,
            "snippet_after": "",
        },
        "rule_id": rule_id,
        "category": None,
        "term": term,
    }


def _occurrences(term: str, text: str, *, eval_name: str = "dictionary",
                 index: int = 0) -> list[dict]:
    """The entries the normalizer fans one repeated-word finding into.

    They share ``issue_index``, ``severity``, ``message`` and ``location.raw``
    — the four fields ``issue_key`` hashes — and differ only in the offset,
    which is exactly the shape the dictionary evaluator produces for a word it
    found more than once in a chunk.
    """
    starts: list[int] = []
    at = text.find(term)
    while at >= 0:
        starts.append(at)
        at = text.find(term, at + 1)
    raw = "Character positions: " + ", ".join(str(s) for s in starts)
    out = []
    for start in starts:
        ni = _ni(eval_name, term, text, index=index,
                 message=f"'{term}': Unknown word (found {len(starts)} time(s))")
        ni["location"]["raw"] = raw
        ni["location"]["char_start"] = start
        ni["location"]["char_end"] = start + len(term)
        out.append(ni)
    return out


@pytest.fixture
def book(tmp_path: Path) -> Path:
    project = tmp_path / "bk"
    _chunk(project, "chapter_01_chunk_000")
    _alignment(project, "chapter_01", "chapter_01_chunk_000")
    _evaluation(project, "chapter_01_chunk_000", [_ni("dictionary", "Sigfridos")])
    return project


def test_finding_carries_the_sentence_its_offset_lands_in(book: Path):
    items, _ = tf.collect_book(book)
    assert len(items) == 1
    item = items[0]
    assert item["term"] == "Sigfridos"
    # The sentence, not the paragraph before it and not the whole chunk.
    assert item["occurrences"] == 1
    assert len(item["sentences"]) == 1
    assert "Sigfridos" in item["sentences"][0]
    assert "El niño escuchaba" not in item["sentences"][0]


def test_a_second_paragraph_finding_gets_its_own_sentence(tmp_path: Path):
    """The join must follow the offset, not just return the first row."""
    project = tmp_path / "bk"
    _chunk(project, "chapter_01_chunk_000")
    _alignment(project, "chapter_01", "chapter_01_chunk_000")
    _evaluation(project, "chapter_01_chunk_000", [_ni("dictionary", "escuchaba")])
    items, _ = tf.collect_book(project)
    assert len(items) == 1
    assert "El niño escuchaba" in items[0]["sentences"][0]
    assert "Fru Astrida" not in items[0]["sentences"][0]


def test_item_id_is_unique_and_carries_its_join_back(book: Path):
    items, _ = tf.collect_book(book)
    item = items[0]
    assert item["id"] == (
        f"{item['chunk_id']}:{item['eval_name']}:{item['issue_index']}:{item['issue_key']}"
    )


def test_only_dictionary_and_grammar_are_collected(tmp_path: Path):
    project = tmp_path / "bk"
    _chunk(project, "chapter_01_chunk_000")
    _alignment(project, "chapter_01", "chapter_01_chunk_000")
    _evaluation(project, "chapter_01_chunk_000", [
        _ni("dictionary", "Sigfridos", index=0),
        _ni("grammar", "escuchaba", index=1, rule_id="MORFOLOGIK_RULE_ES"),
        _ni("blacklist", "duque", index=2),
        _ni("completeness", "fuego", index=3),
    ])
    items, _ = tf.collect_book(project)
    assert {i["eval_name"] for i in items} == {"dictionary", "grammar"}


def test_grammar_finding_keeps_its_rule_id(tmp_path: Path):
    project = tmp_path / "bk"
    _chunk(project, "chapter_01_chunk_000")
    _alignment(project, "chapter_01", "chapter_01_chunk_000")
    _evaluation(project, "chapter_01_chunk_000", [
        _ni("grammar", "escuchaba", rule_id="COMMA_ADVERB", message="Falta una coma."),
    ])
    items, _ = tf.collect_book(project)
    assert items[0]["rule_id"] == "COMMA_ADVERB"


def test_a_dismissed_finding_is_not_sent_again(book: Path):
    from web_ui.evaluations import issue_key
    ni = _ni("dictionary", "Sigfridos")
    append_feedback(
        book, "chapter_01_chunk_000", "dictionary", 0, "false_positive",
        key=issue_key("dictionary", ni),
    )
    items, skips = tf.collect_book(book)
    assert items == []
    assert skips["dismissed"] == 1


def test_an_already_triaged_finding_is_not_sent_again(book: Path):
    """A standing `keep` counts too: the question has been answered."""
    from web_ui.evaluations import issue_key
    ni = _ni("dictionary", "Sigfridos")
    append_triage(
        book, "chapter_01_chunk_000", "dictionary", 0, "keep",
        key=issue_key("dictionary", ni), confidence=0.4,
    )
    items, skips = tf.collect_book(book)
    assert items == []
    assert skips["already_triaged"] == 1


def test_an_unanchorable_finding_is_counted_not_sent(tmp_path: Path):
    """No sentence means no basis for a verdict; it stays live for the human."""
    project = tmp_path / "bk"
    _chunk(project, "chapter_01_chunk_000")
    _alignment(project, "chapter_01", "chapter_01_chunk_000")
    issue = _ni("dictionary", "Sigfridos")
    issue["location"]["char_start"] = 99_999
    issue["location"]["char_end"] = 100_008
    _evaluation(project, "chapter_01_chunk_000", [issue])
    items, skips = tf.collect_book(project)
    assert items == []
    assert skips["unanchored"] == 1


def test_a_source_side_finding_is_ignored(tmp_path: Path):
    project = tmp_path / "bk"
    _chunk(project, "chapter_01_chunk_000")
    _alignment(project, "chapter_01", "chapter_01_chunk_000")
    issue = _ni("dictionary", "Sigfridos")
    issue["location"]["side"] = "source"
    _evaluation(project, "chapter_01_chunk_000", [issue])
    items, _ = tf.collect_book(project)
    assert items == []


def test_a_chapter_without_an_alignment_yields_nothing(tmp_path: Path):
    """The alignment is what carries sentences, so no alignment means no triage."""
    project = tmp_path / "bk"
    _chunk(project, "chapter_01_chunk_000")
    _evaluation(project, "chapter_01_chunk_000", [_ni("dictionary", "Sigfridos")])
    items, _ = tf.collect_book(project)
    assert items == []
    assert list(tf.iter_chapters(project)) == []


def test_chapters_filter_limits_the_walk(tmp_path: Path):
    project = tmp_path / "bk"
    for chapter, chunk in (("chapter_01", "chapter_01_chunk_000"),
                           ("chapter_02", "chapter_02_chunk_000")):
        _chunk(project, chunk)
        _alignment(project, chapter, chunk)
        _evaluation(project, chunk, [_ni("dictionary", "Sigfridos")])
    assert len(tf.collect_book(project)[0]) == 2
    only_first = tf.collect_book(project, chapters=["chapter_01"])[0]
    assert [i["chunk_id"] for i in only_first] == ["chapter_01_chunk_000"]


def test_prompt_view_hides_bookkeeping_and_the_checkers_guess(book: Path):
    """The model sees the question, not the sidecar's keys or the checker's fix."""
    items, _ = tf.collect_book(book)
    view = tf.item_prompt_view(items[0], ["spring → manantial"], number=1)
    assert set(view) == {"item", "eval_name", "term", "message", "sentences", "glossary"}
    assert view["item"] == 1
    # The opaque key never reaches the model: it cannot copy one back reliably.
    assert "id" not in view
    assert "issue_key" not in view and "chunk_id" not in view
    assert "suggestion" not in view
    assert view["glossary"] == ["spring → manantial"]
    assert view["sentences"] == items[0]["sentences"]


def test_a_repeated_word_is_one_item_carrying_every_sentence(tmp_path: Path):
    """The checker reports it once and ``issue_key`` cannot tell the occurrences
    apart, so the prompt must not ask about it three times.

    Rendering one item per occurrence put the same id in a job more than once,
    which ``pass_.parse_draft`` rejects, and let a verdict formed on one sentence
    suppress occurrences the model never saw.
    """
    project = tmp_path / "bk"
    _chunk(project, "chapter_01_chunk_000", REPEATED)
    _alignment(project, "chapter_01", "chapter_01_chunk_000", REPEATED)
    _evaluation(project, "chapter_01_chunk_000", _occurrences("Sigfridos", REPEATED))
    items, _ = tf.collect_book(project)
    assert len(items) == 1
    item = items[0]
    assert item["occurrences"] == 3
    # Each occurrence's own sentence, not the first one repeated.
    assert len(item["sentences"]) == len(set(item["sentences"])) == 3
    assert all("Sigfridos" in sentence for sentence in item["sentences"])


def test_every_collected_id_is_unique(tmp_path: Path):
    """``pass_.parse_draft`` rejects a draft whose ids repeat, so a job that
    rendered one id twice could never be committed at all."""
    project = tmp_path / "bk"
    _chunk(project, "chapter_01_chunk_000", REPEATED)
    _alignment(project, "chapter_01", "chapter_01_chunk_000", REPEATED)
    _evaluation(project, "chapter_01_chunk_000",
                _occurrences("Sigfridos", REPEATED)
                + [_ni("grammar", "duque", REPEATED, index=1)])
    items, _ = tf.collect_book(project)
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids)) == 2


def test_a_dismissed_repeated_word_is_one_skip_not_three(tmp_path: Path):
    """The tally counts findings, not offsets.

    Counting each occurrence made ``skipped.dismissed`` read higher than the
    number of marks a human had actually made, which is the number the report is
    there to convey.
    """
    from web_ui.evaluations import issue_key
    project = tmp_path / "bk"
    _chunk(project, "chapter_01_chunk_000", REPEATED)
    _alignment(project, "chapter_01", "chapter_01_chunk_000", REPEATED)
    occurrences = _occurrences("Sigfridos", REPEATED)
    _evaluation(project, "chapter_01_chunk_000", occurrences)
    append_feedback(
        project, "chapter_01_chunk_000", "dictionary", 0, "false_positive",
        key=issue_key("dictionary", occurrences[0]),
    )
    items, skips = tf.collect_book(project)
    assert items == []
    assert skips["dismissed"] == 1
