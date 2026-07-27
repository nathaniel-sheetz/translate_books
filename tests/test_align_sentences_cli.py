"""CLI helpers for scripts/align_sentences.py."""

from scripts.align_sentences import _print_coverage_gaps


def test_print_coverage_gaps_silent_when_empty(capsys):
    _print_coverage_gaps({"gaps": []})
    _print_coverage_gaps({})
    assert capsys.readouterr().out == ""


def test_print_coverage_gaps_reports_each_run(capsys):
    _print_coverage_gaps({
        "chunk_id": "chapter_01_chunk_000",
        "gaps": [
            {
                "position": "tail",
                "en_start": 45,
                "en_end": 47,
                "sentences": 3,
                "chars": 749,
                "preview": "Richard was bidden to greet them…",
                "chunk_id": "chapter_01_chunk_000",
            },
        ],
    })
    out = capsys.readouterr().out
    assert "COVERAGE GAPS: 1" in out
    assert "chapter_01_chunk_000 tail" in out
    assert "749 chars" in out
    assert "EN 45-47" in out
    assert "Richard was bidden" in out
