"""Tests for multi-judge ``prepare`` and the ``run_judges`` output contract.

Two friction families, both logged five-plus times and both living in this CLI:

1. ``prepare`` could name only ONE judge, so "run both judges" cost two full
   prepare -> fan-out -> commit cycles — two consent gates, two waves each paying
   the fixed per-process baseline, two usage rollups to reconcile by hand. And
   even repeatable ``--judge`` is not enough on its own: the real request is
   asymmetric ("dialogue on the ten chapters that owe it, address on the eight
   that owe *that*"), which a shared scope can only serve by re-judging clean work.
2. ``commit`` echoed one full ``EvalResult`` per pair with no way to suppress it
   and no copy on disk, so a 20-chapter commit truncated mid-``results[]`` and the
   agent hand-rolled a ``python -c`` filter to recover — the step that mangled
   every raya on Windows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.judges import subagent
from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import save_chunk

run_judges = pytest.importorskip("scripts.run_judges")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _chunk(cid: str, chapter: str) -> Chunk:
    return Chunk(
        id=cid,
        chapter_id=chapter,
        position=0,
        source_text='"Hello," he said.',
        translated_text="—Hola —dijo él.",
        metadata=ChunkMetadata(
            char_start=0, char_end=17, overlap_start=0, overlap_end=0,
            paragraph_count=1, word_count=3,
        ),
        status=ChunkStatus.TRANSLATED,
    )


@pytest.fixture
def project(tmp_path) -> Path:
    """A two-chapter book with the address map the address judge requires."""
    proj = tmp_path / "projects" / "multijudge"
    (proj / "chunks").mkdir(parents=True)
    for chapter in ("chapter_01", "chapter_02"):
        save_chunk(
            _chunk(f"{chapter}_chunk_000", chapter),
            proj / "chunks" / f"{chapter}_chunk_000.json",
        )
    (proj / "address_map.json").write_text(
        json.dumps({
            "content": "Betsy->Frances usted; Frances->Betsy tú.",
            "pairs": [],
            "global_rules": "usted between non-intimate adults.",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return proj


def _prepare(capsys, project: Path, *args) -> dict:
    rc = run_judges.main(["prepare", "--project", str(project), *args])
    out = json.loads(capsys.readouterr().out)
    return {"rc": rc, **out}


def _pairs(payload: dict) -> set:
    return {(e["target_id"], e["judge"]) for e in payload["manifest"]}


# ---------------------------------------------------------------------------
# 1. Repeatable --judge on prepare
# ---------------------------------------------------------------------------


def test_two_judges_render_into_one_manifest(project, capsys):
    """One prepare replaces what used to be two whole cycles."""
    out = _prepare(
        capsys, project, "--judge", "dialogue", "--judge", "address", "--scope", "book"
    )

    assert out["rc"] == 0 and out["status"] == "ok"
    assert out["judges"] == ["dialogue", "address"]
    assert _pairs(out) == {
        ("chapter_01_chunk_000", "dialogue"),
        ("chapter_02_chunk_000", "dialogue"),
        ("chapter_01_chunk_000", "address"),
        ("chapter_02_chunk_000", "address"),
    }
    # pairs is the sum over judges; targets is the union (each chunk once).
    assert out["usage_summary"]["pairs"] == 4
    assert out["usage_summary"]["targets"] == 2

    manifest = json.loads(
        (project / ".harness" / "judges" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["judges"] == ["dialogue", "address"]
    assert manifest["scopes_by_judge"] == {"dialogue": ["book"], "address": ["book"]}
    # The flat union stays for anything reading an older manifest.
    assert manifest["scopes"] == ["book"]


def test_repeated_judge_is_deduped_but_keeps_order(project, capsys):
    out = _prepare(
        capsys, project,
        "--judge", "address", "--judge", "dialogue", "--judge", "address",
        "--scope", "chapter:chapter_01",
    )
    assert out["judges"] == ["address", "dialogue"]


def test_judge_and_suite_together_is_rejected(project, capsys):
    out = _prepare(
        capsys, project, "--judge", "dialogue", "--suite", "default", "--scope", "book"
    )
    assert out["rc"] == 1
    assert "mutually exclusive" in out["error"]


def test_neither_judge_nor_suite_is_rejected(project, capsys):
    out = _prepare(capsys, project, "--scope", "book")
    assert out["rc"] == 1
    assert "required" in out["error"]


def test_prose_suite_runs_both_judges(project, capsys):
    out = _prepare(capsys, project, "--suite", "prose", "--scope", "chapter:chapter_01")
    assert out["judges"] == ["dialogue", "address"]
    assert _pairs(out) == {
        ("chapter_01_chunk_000", "dialogue"),
        ("chapter_01_chunk_000", "address"),
    }


# ---------------------------------------------------------------------------
# 2. Per-judge scope tagging
# ---------------------------------------------------------------------------


def test_tagged_scopes_bind_to_one_judge_each(project, capsys):
    """The 08-09 request: both judges, different chapters, no cross-product."""
    out = _prepare(
        capsys, project,
        "--judge", "dialogue", "--judge", "address",
        "--scope", "dialogue:book",
        "--scope", "address:chapter:chapter_02",
    )

    assert out["rc"] == 0
    assert _pairs(out) == {
        ("chapter_01_chunk_000", "dialogue"),
        ("chapter_02_chunk_000", "dialogue"),
        ("chapter_02_chunk_000", "address"),
    }
    assert out["scopes_by_judge"] == {
        "dialogue": ["book"],
        "address": ["chapter:chapter_02"],
    }
    assert out["usage_summary"]["pairs"] == 3
    assert out["usage_summary"]["targets"] == 2


def test_untagged_scope_still_applies_to_every_judge(project, capsys):
    """The old meaning is unchanged — a bare scope covers all named judges."""
    out = _prepare(
        capsys, project,
        "--judge", "dialogue", "--judge", "address",
        "--scope", "chapter:chapter_01",
    )
    assert _pairs(out) == {
        ("chapter_01_chunk_000", "dialogue"),
        ("chapter_01_chunk_000", "address"),
    }


def test_tagged_and_untagged_scopes_mix(project, capsys):
    out = _prepare(
        capsys, project,
        "--judge", "dialogue", "--judge", "address",
        "--scope", "chapter:chapter_01",
        "--scope", "dialogue:chapter:chapter_02",
    )
    assert _pairs(out) == {
        ("chapter_01_chunk_000", "dialogue"),
        ("chapter_02_chunk_000", "dialogue"),
        ("chapter_01_chunk_000", "address"),
    }


def test_tagged_scope_is_matched_case_insensitively(project, capsys):
    out = _prepare(
        capsys, project,
        "--judge", "address",
        "--scope", "ADDRESS:chapter:chapter_01",
    )
    assert out["rc"] == 0
    assert out["scopes_by_judge"] == {"address": ["chapter:chapter_01"]}


def test_tag_naming_a_judge_outside_the_run_is_an_error(project, capsys):
    """Never a silent no-op: it would stage less work than was asked for."""
    out = _prepare(
        capsys, project, "--judge", "dialogue", "--scope", "address:chapter:chapter_01"
    )
    assert out["rc"] == 1
    assert "address" in out["error"]
    assert not (project / ".harness" / "judges" / "manifest.json").exists()


def test_tag_naming_an_unregistered_judge_is_an_error(project, capsys):
    out = _prepare(
        capsys, project,
        "--judge", "dialogue", "--judge", "address",
        "--scope", "nosuchjudge:book",
    )
    assert out["rc"] == 1
    assert "nosuchjudge" in out["error"]


def test_a_judge_left_with_no_scope_is_an_error(project, capsys):
    """Tagging every scope to one judge starves the other — say so, don't render it."""
    out = _prepare(
        capsys, project,
        "--judge", "dialogue", "--judge", "address",
        "--scope", "dialogue:book",
    )
    assert out["rc"] == 1
    assert "address" in out["error"]


def test_run_rejects_a_tagged_scope_that_would_starve(project, capsys):
    """A tag on a multi-judge `run` would starve the untagged judge."""
    rc = run_judges.main(
        [
            "run", "--project", str(project),
            "--judge", "dialogue", "--judge", "address",
            "--scope", "dialogue:book",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "prepare" in out["error"]


def test_run_scope_strips_a_matching_single_judge_tag():
    """Prepare copy-paste (`--judge address --scope address:chapter:x`) is fine."""
    assert run_judges._run_scope("address:chapter:chapter_04", ["address"]) == (
        "chapter:chapter_04"
    )
    assert run_judges._run_scope("ADDRESS:chapter:chapter_04", ["address"]) == (
        "chapter:chapter_04"
    )
    assert run_judges._run_scope("chapter:chapter_04", ["address"]) == (
        "chapter:chapter_04"
    )
    with pytest.raises(ValueError, match="prepare"):
        run_judges._run_scope("dialogue:book", ["address"])
    with pytest.raises(ValueError, match="prepare"):
        run_judges._run_scope("dialogue:book", ["dialogue", "address"])


def test_scope_tag_helper_leaves_plain_scopes_alone():
    assert run_judges._split_scope_tag("book") == (None, "book")
    assert run_judges._split_scope_tag("chapter:chapter_03") == (None, "chapter:chapter_03")
    assert run_judges._split_scope_tag("address:chapter:chapter_03") == (
        "address",
        "chapter:chapter_03",
    )
    assert run_judges._split_scope_tag("ADDRESS:chapter:chapter_03") == (
        "address",
        "chapter:chapter_03",
    )


# ---------------------------------------------------------------------------
# 3. The output contract: --brief + the last_output.json sidecar
# ---------------------------------------------------------------------------


_VERDICT = json.dumps(
    {
        "compliant": False,
        "findings": [
            {
                "rule": "raya-spacing",
                "severity": "error",
                "excerpt": "— Hola, ¿qué tal?",
                "message": "space after the opening raya",
                "suggestion": "—Hola, ¿qué tal?",
            }
        ],
        "summary": "one issue",
    },
    ensure_ascii=False,
)


def _drafted(project: Path, capsys) -> None:
    """Prepare one chunk for one judge and answer it with a real verdict."""
    _prepare(capsys, project, "--judge", "dialogue", "--scope", "chapter:chapter_01")
    (project / ".harness" / "judges" / "chapter_01_chunk_000.dialogue.draft.json").write_text(
        _VERDICT, encoding="utf-8"
    )


def test_commit_brief_drops_results_but_keeps_the_rollup(project, capsys):
    _drafted(project, capsys)
    rc = run_judges.main(["commit", "--project", str(project), "--brief"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert "results" not in out
    row = out["results_brief"][0]
    assert len(out["results_brief"]) == 1
    assert row["target_id"] == "chapter_01_chunk_000"
    assert row["judge"] == "dialogue"
    assert row["passed"] is False
    assert (row["errors"], row["warnings"], row["info"]) == (1, 0, 0)
    # Everything the caller still needs to act survives the abridgement.
    for key in ("summary", "counts", "run_header", "persisted_dir", "persisted"):
        assert key in out


def test_commit_without_brief_is_unchanged(project, capsys):
    _drafted(project, capsys)
    rc = run_judges.main(["commit", "--project", str(project)])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert "results_brief" not in out
    assert out["results"][0]["issues"][0]["message"]


def test_sidecar_holds_the_full_payload_with_readable_rayas(project, capsys):
    """--brief is only safe because the findings land on disk, in real UTF-8."""
    _drafted(project, capsys)
    run_judges.main(["commit", "--project", str(project), "--brief"])
    capsys.readouterr()

    sidecar = project / ".harness" / "judges" / "last_output.json"
    text = sidecar.read_text(encoding="utf-8")
    saved = json.loads(text)
    assert saved["results"][0]["issues"][0]["suggestion"] == "—Hola, ¿qué tal?"
    # The bytes on disk are UTF-8, not an escaped ASCII transcription.
    assert "—Hola, ¿qué tal?" in text
    # `--brief` used to dump `_COMMIT_SCHEMA` here because `full=` bypassed the
    # stdout schema strip. The findings stay; the docs block does not.
    assert "results" in saved
    assert "_schema" not in saved


def test_sidecar_is_refreshed_per_command(project, capsys):
    """Never the previous command's answer left in place."""
    _prepare(capsys, project, "--judge", "dialogue", "--scope", "chapter:chapter_01")
    sidecar = project / ".harness" / "judges" / "last_output.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["manifest_path"]

    run_judges.main(["commit", "--project", str(project)])
    capsys.readouterr()
    assert "manifest_path" not in json.loads(sidecar.read_text(encoding="utf-8"))


def test_read_only_commands_write_no_sidecar(project, capsys):
    """`status` and `profile` promise no writes — the sidecar must not break that."""
    run_judges.main(["status", "--project", str(project)])
    run_judges.main(["profile", "--project", str(project)])
    capsys.readouterr()
    assert not (project / ".harness" / "judges").exists()


def test_brief_results_helper_maps_the_counts():
    rows = subagent.brief_results([
        {
            "target_id": "c0", "eval_name": "address", "score": 0.5, "passed": False,
            "error_count": 2, "warning_count": 1, "info_count": 0,
        }
    ])
    assert rows == [
        {
            "target_id": "c0", "judge": "address", "score": 0.5, "passed": False,
            "errors": 2, "warnings": 1, "info": 0,
        }
    ]


def test_quiet_prepare_sidecar_keeps_the_manifest(project, capsys):
    """`--quiet` abridges stdout; last_output.json still holds `manifest`."""
    rc = run_judges.main([
        "prepare", "--project", str(project),
        "--judge", "dialogue", "--scope", "chapter:chapter_01", "--quiet",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "manifest" not in out
    assert out["manifest_entries"] == 1

    sidecar = json.loads(
        (project / ".harness" / "judges" / "last_output.json").read_text(encoding="utf-8")
    )
    assert len(sidecar["manifest"]) == out["manifest_entries"]
    assert "_schema" not in sidecar


def test_sidecar_write_failure_prints_a_warning(project, capsys, monkeypatch):
    original = Path.write_text

    def _maybe_boom(self, *args, **kwargs):
        if self.name == "last_output.json":
            raise OSError("disk full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _maybe_boom)
    rc = run_judges.main([
        "prepare", "--project", str(project),
        "--judge", "dialogue", "--scope", "chapter:chapter_01",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["status"] == "ok"
    assert "warning" in captured.err.lower()
    assert "last_output.json" in captured.err


def test_bare_judge_name_as_scope_is_an_error(project, capsys):
    """`--scope address` is a tag with an empty rest, not a shorthand for book."""
    out = _prepare(capsys, project, "--judge", "address", "--scope", "address")
    assert out["rc"] == 1
    assert "address" in out["error"]
