"""Tests for the editorial adjudication pass and its English-window retrieval.

Two properties carry most of the risk here:

* **A window must never present the wrong English as the source.** A verifier
  shown an unrelated passage adjudicates confidently against it, which is worse
  than showing it nothing. The contiguous-run rule in ``_match_rows`` is what
  stops one short sentence appearing elsewhere in the chunk from anchoring the
  window — a real ``gaudenzia`` case stretched one window to 181 rows.
* **A missing or malformed verdict must not silently delete a finding.** The
  pass exists to retract false positives on the record; losing findings by
  accident would look identical in the metrics and mean the opposite.
"""

from __future__ import annotations

import json

import pytest

from src.judges import editorial_verify as ev
from src.judges import llm_io
from src.judges.editorial_judge import _disambiguate_keys
from src.judges.neighborhood import MAX_WINDOW_ROWS, english_window, load_alignment_rows

ES = [
    "Pollyanna subió al ático.",
    "El cuarto estaba desnudo y caluroso.",
    "Miró por la ventana y sonrió.",
    "El Paragüero",
    "Nancy la llamó desde abajo.",
    "—Ya voy —respondió.",
]
EN = [
    "Pollyanna climbed to the attic.",
    "The room was bare and hot.",
    "She looked out of the window and smiled.",
    "The Umbrella Man",
    "Nancy called her from below.",
    '"Coming," she answered.',
]


def _alignment(tmp_path, chapter="chapter_01", chunk="chapter_01_chunk_000", confidences=None):
    rows = []
    for index, (es, en) in enumerate(zip(ES, EN)):
        rows.append(
            {
                "es_idx": index,
                "en_idx": index,
                "es": es,
                "en": en,
                "similarity": 0.9,
                "confidence": (confidences or {}).get(index, "high"),
                "chunk_id": chunk,
            }
        )
    directory = tmp_path / "alignments"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{chapter}.json").write_text(
        json.dumps({"chapter_id": chapter, "alignments": rows}, ensure_ascii=False),
        encoding="utf-8",
    )
    return rows


def _result(findings):
    return {
        "eval_name": "editorial",
        "eval_version": "1.0.0",
        "target_id": "chapter_01_chunk_000",
        "target_type": "chunk",
        "passed": True,
        "score": 1.0,
        "issues": [],
        "metadata": {"candidates": findings, "verified": False},
    }


def _finding(**overrides):
    finding = {
        "rule": "calque-syntax",
        "category": "NATURALNESS",
        "severity": "warning",
        "confidence": "high",
        "excerpt": "El cuarto estaba desnudo y caluroso.",
        "message": "Reads as a calque.",
        "suggestion": "El cuarto era estrecho y sofocante.",
        "source_check": "not_needed",
    }
    finding.update(overrides)
    return finding


# ---------------------------------------------------------------------------
# English window


def test_window_finds_the_english_around_an_excerpt(tmp_path):
    _alignment(tmp_path)

    window = english_window(
        tmp_path, "chapter_01", "El cuarto estaba desnudo y caluroso.",
        chunk_id="chapter_01_chunk_000",
    )

    assert window.matched
    assert window.method == "contains"
    assert "The room was bare and hot." in window.english_text()
    assert "Pollyanna climbed to the attic." in window.english_text()


def test_window_matches_despite_missing_accents(tmp_path):
    """Folded matching, because a judge reproduces letters more reliably than accents."""
    _alignment(tmp_path)

    window = english_window(
        tmp_path, "chapter_01", "Miro por la ventana y sonrio.",
        chunk_id="chapter_01_chunk_000",
    )

    assert window.matched
    assert "She looked out of the window and smiled." in window.english_text()


def test_a_short_sentence_elsewhere_does_not_stretch_the_window(tmp_path):
    """The gaudenzia case: "El Paragüero" at index 3 must not anchor a span from 0."""
    rows = _alignment(tmp_path)
    # An excerpt covering the last two rows that also contains the row-3 text.
    excerpt = "El Paragüero Nancy la llamó desde abajo. —Ya voy —respondió."

    window = english_window(
        tmp_path, "chapter_01", excerpt, chunk_id="chapter_01_chunk_000", rows=rows, window=0
    )

    assert window.matched
    assert window.es_idx_start == 3
    assert window.es_idx_end == 5


def test_window_is_capped(tmp_path):
    rows = _alignment(tmp_path)
    window = english_window(
        tmp_path, "chapter_01", ES[1], chunk_id="chapter_01_chunk_000", rows=rows, window=500
    )

    assert len(window.rows) <= MAX_WINDOW_ROWS


def test_low_confidence_row_widens_the_window(tmp_path):
    rows = _alignment(tmp_path, confidences={1: "low"})

    narrow = english_window(tmp_path, "chapter_01", ES[1], rows=rows, window=0)

    assert narrow.confidence == "low"
    assert len(narrow.rows) > 1


def test_an_unlocatable_excerpt_returns_no_window(tmp_path):
    _alignment(tmp_path)

    window = english_window(
        tmp_path, "chapter_01", "Una frase que no está en ninguna parte del libro.",
        chunk_id="chapter_01_chunk_000",
    )

    assert not window.matched
    assert window.rows == []
    assert window.english_text() == ""


def test_a_very_short_excerpt_is_not_matched(tmp_path):
    """Below the floor a folded search hits everywhere; a wrong window is worse than none."""
    _alignment(tmp_path)

    assert not english_window(tmp_path, "chapter_01", "el", chunk_id="chapter_01_chunk_000").matched


def test_missing_alignment_file_is_not_fatal(tmp_path):
    assert load_alignment_rows(tmp_path, "chapter_99") == []
    assert not english_window(tmp_path, "chapter_99", "cualquier cosa").matched


# ---------------------------------------------------------------------------
# Attaching context


def test_english_is_attached_only_where_the_finding_asked(tmp_path):
    _alignment(tmp_path)
    result = _result(
        [
            _finding(source_check="not_needed"),
            _finding(
                rule="odd-connector",
                category="FIDELITY_SUSPECT",
                source_check="required",
                excerpt="Miró por la ventana y sonrió.",
            ),
        ]
    )

    candidates = ev.attach_context(
        tmp_path, ev.collect_candidates(result, "chapter_01_chunk_000", " ".join(ES))
    )

    assert candidates[0]["_english_context"] == ""
    assert candidates[0]["_source_requested"] is False
    assert "She looked out of the window" in candidates[1]["_english_context"]
    assert candidates[1]["_source_available"] is True


def test_spanish_context_falls_back_to_the_chunk_when_unaligned(tmp_path):
    """No alignment must not mean no adjudication — the Spanish is still judgeable."""
    text = " ".join(ES)
    result = _result([_finding()])

    candidates = ev.attach_context(
        tmp_path, ev.collect_candidates(result, "chapter_01_chunk_000", text)
    )

    assert candidates[0]["_english_context"] == ""
    assert "El cuarto estaba desnudo" in candidates[0]["_spanish_context"]


def test_prompt_says_so_when_a_requested_window_is_unavailable(tmp_path):
    result = _result([_finding(source_check="required", excerpt="No aparece en el texto.")])
    candidates = ev.attach_context(
        tmp_path, ev.collect_candidates(result, "chapter_01_chunk_000", " ".join(ES))
    )

    # The rendered candidate, not the whole prompt: the instructions name the tag too.
    block = ev._candidate_block(candidates[0])

    assert "<english_context_unavailable>" in block
    assert "<english_context>" not in block
    assert block in ev.build_prompt(candidates, {})


def test_prompt_parts_are_byte_identical_to_the_whole(tmp_path):
    _alignment(tmp_path)
    result = _result([_finding()])
    candidates = ev.attach_context(
        tmp_path, ev.collect_candidates(result, "chapter_01_chunk_000", " ".join(ES))
    )
    ctx = {"style_guide": "Mexican Spanish.", "glossary": "none"}

    prefix, suffix = ev.build_prompt_parts(candidates, ctx)

    assert prefix + suffix == ev.build_prompt(candidates, ctx)
    assert "Mexican Spanish." in prefix
    assert "<candidates>" in suffix


def test_candidate_keys_match_the_dismissal_key(tmp_path):
    """A verdict and a human mark must name the same finding."""
    from src.judges.scoring import finding_key

    result = _result([_finding()])
    candidates = ev.collect_candidates(result, "chapter_01_chunk_000", " ".join(ES))

    assert candidates[0]["key"] == finding_key(
        "calque-syntax", "El cuarto estaba desnudo y caluroso."
    )


def test_candidates_are_reconstructed_from_issues_when_metadata_predates_them(tmp_path):
    """An early wave written before metadata.candidates existed is still verifiable."""
    result = {
        "metadata": {},
        "issues": [
            {
                "severity": "warning",
                "message": "[calque-syntax] Reads as a calque.",
                "location": "El cuarto estaba desnudo y caluroso.",
                "suggestion": "otra cosa",
                "rule_id": "calque-syntax",
                "category": "NATURALNESS",
            }
        ],
    }

    candidates = ev.collect_candidates(result, "chapter_01_chunk_000", " ".join(ES))

    assert len(candidates) == 1
    assert candidates[0]["rule"] == "calque-syntax"


# ---------------------------------------------------------------------------
# Applying verdicts


def _candidates(tmp_path, findings):
    _alignment(tmp_path)
    return ev.attach_context(
        tmp_path, ev.collect_candidates(_result(findings), "chapter_01_chunk_000", " ".join(ES))
    )


def test_retract_removes_the_finding_and_records_why(tmp_path):
    findings = [_finding()]
    candidates = _candidates(tmp_path, findings)
    verdicts = {
        candidates[0]["key"]: {
            "verdict": "RETRACT",
            "reason": "idiomatic after all",
            "used_source": False,
        }
    }

    patched = ev.apply_verdicts(_result(findings), candidates, verdicts)

    assert patched["issues"] == []
    assert patched["metadata"]["retracted_count"] == 1
    assert patched["metadata"]["retracted"][0]["reason"] == "idiomatic after all"
    assert patched["metadata"]["verified"] is True


def test_candidates_are_preserved_so_the_retract_rate_is_measurable(tmp_path):
    findings = [_finding()]
    candidates = _candidates(tmp_path, findings)
    verdicts = {candidates[0]["key"]: {"verdict": "RETRACT", "reason": "no"}}

    patched = ev.apply_verdicts(_result(findings), candidates, verdicts)

    assert len(patched["metadata"]["candidates"]) == 1
    assert patched["metadata"]["candidates_adjudicated"] == 1


def test_reclassify_rewrites_the_fields_it_names(tmp_path):
    findings = [_finding(severity="error")]
    candidates = _candidates(tmp_path, findings)
    verdicts = {
        candidates[0]["key"]: {
            "verdict": "RECLASSIFY",
            "reason": "style, not grammar",
            "category": "STYLE_GUIDE",
            "severity": "warning",
            "message": "Register slips out of period.",
        }
    }

    patched = ev.apply_verdicts(_result(findings), candidates, verdicts)

    assert len(patched["issues"]) == 1
    assert patched["issues"][0]["category"] == "STYLE_GUIDE"
    assert patched["issues"][0]["severity"] == "warning"
    assert "Register slips" in patched["issues"][0]["message"]
    assert patched["passed"] is True
    assert patched["metadata"]["reclassified"] == 1


def test_confirm_keeps_the_finding_and_can_improve_the_fix(tmp_path):
    findings = [_finding()]
    candidates = _candidates(tmp_path, findings)
    verdicts = {
        candidates[0]["key"]: {
            "verdict": "CONFIRM",
            "reason": "genuine calque",
            "suggestion": "El cuarto era angosto y sofocante.",
        }
    }

    patched = ev.apply_verdicts(_result(findings), candidates, verdicts)

    assert len(patched["issues"]) == 1
    assert patched["issues"][0]["suggestion"] == "El cuarto era angosto y sofocante."
    assert patched["metadata"]["confirmed"] == 1


def test_a_missing_verdict_keeps_the_finding(tmp_path):
    """An omitted key has not retracted anything; dropping it would overstate the pass."""
    findings = [_finding()]
    candidates = _candidates(tmp_path, findings)

    patched = ev.apply_verdicts(_result(findings), candidates, {})

    assert len(patched["issues"]) == 1
    assert patched["metadata"]["retracted_count"] == 0


def test_an_unknown_verdict_is_read_as_confirm():
    """Conservative: never delete a finding on a value we could not parse."""
    verdicts = ev.parse_verdicts(json.dumps({"verdicts": {"k": {"verdict": "MAYBE"}}}))

    assert verdicts["k"]["verdict"] == "CONFIRM"


def test_source_used_is_only_counted_where_english_was_actually_attached(tmp_path):
    findings = [_finding(source_check="not_needed")]
    candidates = _candidates(tmp_path, findings)
    verdicts = {candidates[0]["key"]: {"verdict": "CONFIRM", "used_source": True}}

    patched = ev.apply_verdicts(_result(findings), candidates, verdicts)

    assert patched["metadata"]["source_attached"] == 0
    assert patched["metadata"]["source_used"] == 0


def test_working_fields_are_not_persisted(tmp_path):
    findings = [_finding()]
    candidates = _candidates(tmp_path, findings)
    verdicts = {candidates[0]["key"]: {"verdict": "CONFIRM"}}

    patched = ev.apply_verdicts(_result(findings), candidates, verdicts)
    blob = json.dumps(patched, ensure_ascii=False)

    assert "_spanish_context" not in blob
    assert "_english_context" not in blob
    assert "_translated_text" not in blob


# ---------------------------------------------------------------------------
# Parsing and the API path


def test_parse_verdicts_rejects_a_missing_verdicts_object():
    with pytest.raises(llm_io.JudgeParseError):
        ev.parse_verdicts(json.dumps({"findings": []}))

    with pytest.raises(llm_io.JudgeParseError):
        ev.parse_verdicts(json.dumps({"verdicts": []}))


def test_verify_result_leaves_pass_one_untouched_on_a_parse_error(tmp_path):
    findings = [_finding()]
    result = _result(findings)
    _alignment(tmp_path)

    patched, info = ev.verify_result(
        tmp_path,
        "chapter_01_chunk_000",
        result,
        " ".join(ES),
        {},
        call=lambda prompt, **kw: "not json",
    )

    assert info["status"] == "parse_error"
    assert patched is result
    assert patched["metadata"]["verified"] is False


def test_verify_result_reports_no_candidates_without_calling_the_llm(tmp_path):
    calls = []

    patched, info = ev.verify_result(
        tmp_path,
        "chapter_01_chunk_000",
        _result([]),
        " ".join(ES),
        {},
        call=lambda prompt, **kw: calls.append(prompt) or "{}",
    )

    assert info["status"] == "no_candidates"
    assert calls == []


def test_verify_result_round_trip(tmp_path):
    findings = [_finding(), _finding(rule="agreement", excerpt="Miró por la ventana y sonrió.")]
    result = _result(findings)
    _alignment(tmp_path)
    candidates = ev.attach_context(
        tmp_path, ev.collect_candidates(result, "chapter_01_chunk_000", " ".join(ES))
    )

    def fake(prompt, **kwargs):
        assert "<candidate key=" in prompt
        return json.dumps(
            {
                "verdicts": {
                    candidates[0]["key"]: {"verdict": "RETRACT", "reason": "fine"},
                    candidates[1]["key"]: {"verdict": "CONFIRM", "reason": "real"},
                }
            }
        )

    patched, info = ev.verify_result(
        tmp_path, "chapter_01_chunk_000", result, " ".join(ES), {}, call=fake
    )

    assert info["status"] == "ok"
    assert info["adjudicated"] == 2
    assert info["retracted"] == 1
    assert info["confirmed"] == 1
    assert len(patched["issues"]) == 1


def test_a_retract_on_one_twin_does_not_delete_the_other(tmp_path):
    """Two defects quoting one sentence are two findings, settled one at a time.

    Before ``_disambiguate_keys`` both hashed to the same ``finding_key``, so a
    single RETRACT verdict removed both and reported ``retracted_count: 2`` —
    the surviving real defect deleted with no trace but a duplicated record.
    """
    findings = _disambiguate_keys(
        [
            _finding(message="Reads as a calque."),
            _finding(message="Drops the emphasis of the original.", category="FIDELITY_SUSPECT"),
        ]
    )
    candidates = _candidates(tmp_path, findings)
    assert candidates[0]["key"] != candidates[1]["key"]

    verdicts = {candidates[0]["key"]: {"verdict": "RETRACT", "reason": "idiomatic after all"}}
    patched = ev.apply_verdicts(_result(findings), candidates, verdicts)

    assert patched["metadata"]["retracted_count"] == 1
    assert len(patched["issues"]) == 1
    assert "emphasis" in patched["issues"][0]["message"]
    # The ordinal has to survive ``_clean`` into the re-derived issue, or a
    # dismissal recorded against the survivor stops resolving after pass 2.
    assert patched["issues"][0]["finding_key"] == candidates[1]["key"]


def test_an_unanswered_candidate_is_not_counted_as_confirmed(tmp_path):
    """``confirmed`` feeds the retract rate; "the model lost it" is not "it agreed".

    A candidate whose key the adjudicator omitted is deliberately kept, so it
    lands in ``survivors`` — and counting the whole leftover set as confirmed
    fed ``editorial_metrics`` a precision number better than the run earned.
    """
    findings = [_finding(rule="calque-syntax"), _finding(rule="agreement")]
    candidates = _candidates(tmp_path, findings)
    verdicts = {candidates[0]["key"]: {"verdict": "CONFIRM", "reason": "genuine"}}

    patched = ev.apply_verdicts(_result(findings), candidates, verdicts)

    assert patched["metadata"]["candidates_adjudicated"] == 2
    assert patched["metadata"]["confirmed"] == 1
    assert patched["metadata"]["unanswered"] == 1
    assert len(patched["issues"]) == 2
