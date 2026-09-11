"""The CLI envelope: one JSON object per subcommand, exit codes, the sidecar.

Two of these are about Windows specifically, because that is where this repo runs.
``--chapters`` accepts the numeric range form the rest of the repo's CLIs take, and
stdout is reconfigured to UTF-8 so a raya survives the console codepage.
"""

from __future__ import annotations

import json

import pytest

from scripts import footnote_pass as cli

from .conftest import footnote_record, write_annotations


def _run(capsys, argv):
    """Run the CLI, returning ``(exit_code, parsed_stdout)``.

    The captured stderr is stashed on the function so a test can assert on the
    ``OUTPUT_JSON:`` pointer without a second ``readouterr`` (which would come
    back empty — the first call drains both streams).
    """
    code = cli.main(argv)
    captured = capsys.readouterr()
    _run.stderr = captured.err
    return code, json.loads(captured.out)


# --- --chapters parsing ---------------------------------------------------

@pytest.mark.parametrize(
    "spec,expected",
    [
        ("1-3", ["chapter_01", "chapter_02", "chapter_03"]),
        ("3,7,12", ["chapter_03", "chapter_07", "chapter_12"]),
        ("1-2,7", ["chapter_01", "chapter_02", "chapter_07"]),
        ("chapter_04", ["chapter_04"]),
        ("chapter_04,9", ["chapter_04", "chapter_09"]),
        ("2,2,2", ["chapter_02"]),
        (None, None),
        ("", None),
    ],
)
def test_parse_chapters(spec, expected):
    assert cli._parse_chapters(spec) == expected


@pytest.mark.parametrize("spec", ["abc", "1-x", "5-2"])
def test_parse_chapters_rejects_nonsense(spec):
    with pytest.raises(SystemExit) as exc:
        cli._parse_chapters(spec)
    assert json.loads(str(exc.value))["status"] == "error"


# --- the envelope ---------------------------------------------------------

def test_style_prints_one_json_object_and_mirrors_it(project, capsys):
    write_annotations(project, [footnote_record("chapter_01", 0, "[ostión] Molusco.")])
    code, out = _run(capsys, ["style", "--project", str(project)])
    assert code == 0
    assert out["counts"]["glosses"] == 1

    sidecar = project / ".harness" / "footnotes" / "last_output.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["counts"]["glosses"] == 1
    # The pointer goes to stderr, so stdout stays a single parseable JSON object.
    assert f"OUTPUT_JSON: {sidecar}" in _run.stderr


def test_the_sidecar_never_carries_the_schema(project, capsys):
    _run(capsys, ["verify", "--project", str(project)])
    sidecar = json.loads(
        (project / ".harness" / "footnotes" / "last_output.json").read_text(encoding="utf-8")
    )
    assert "_schema" not in sidecar


def test_an_unknown_project_is_one_json_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["style", "--project", "no-such-book"])
    payload = json.loads(str(exc.value))
    assert payload["status"] == "error"
    assert "no-such-book" in payload["error"]


def test_verify_exits_nonzero_when_a_note_is_broken(project, capsys):
    write_annotations(project, [footnote_record("chapter_01", 99, "[x] Huérfana.")])
    code, out = _run(capsys, ["verify", "--project", str(project)])
    assert code == 1
    assert out["status"] == "broken"


def test_verify_accepts_a_chapter_range(project, capsys):
    write_annotations(
        project,
        [
            footnote_record("chapter_01", 99, "[x] Huérfana.", sub_id="u1"),
            footnote_record("chapter_02", 0, "[colmena] Caja.", sub_id="u2"),
        ],
    )
    code, out = _run(capsys, ["verify", "--project", str(project), "--chapters", "2"])
    assert code == 0
    assert out["counts"]["audited"] == 1


# --- add ------------------------------------------------------------------

def test_add_needs_the_whole_triple(project, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["add", "--project", str(project), "--chapter", "chapter_01"])
    payload = json.loads(str(exc.value))
    assert "--es-idx" in payload["error"] and "--note" in payload["error"]


def test_add_refuses_json_file_mixed_with_inline_flags(project, tmp_path, capsys):
    path = tmp_path / "approved.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "add",
                "--project",
                str(project),
                "--json-file",
                str(path),
                "--chapter",
                "chapter_01",
            ]
        )
    assert "exclusive" in json.loads(str(exc.value))["error"]


def test_add_dry_run_writes_nothing_and_exits_zero(project, capsys):
    code, out = _run(
        capsys,
        [
            "add",
            "--project",
            str(project),
            "--chapter",
            "chapter_01",
            "--es-idx",
            "1",
            "--anchor",
            "cuernitos",
            "--note",
            "La gota sale del ano.",
            "--dry-run",
        ],
    )
    assert code == 0
    assert out["dry_run"] is True
    assert not (project / "annotations.jsonl").exists()


def test_add_exits_nonzero_on_a_partial_batch(project, tmp_path, capsys):
    path = tmp_path / "approved.json"
    path.write_text(
        json.dumps(
            [
                {"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": "Bien."},
                {"chapter_id": "chapter_01", "es_idx": 99, "note": "Huérfana."},
            ]
        ),
        encoding="utf-8",
    )
    code, out = _run(capsys, ["add", "--project", str(project), "--json-file", str(path)])
    assert code == 1
    assert out["status"] == "partial"
    assert out["counts"] == {
        "requested": 2,
        "added": 1,
        "planned": 0,
        "refused": 1,
        "warnings": 0,
    }


def test_add_json_file_accepts_a_notes_wrapper(project, tmp_path, capsys):
    path = tmp_path / "approved.json"
    path.write_text(
        json.dumps(
            {"notes": [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": "Bien."}]}
        ),
        encoding="utf-8",
    )
    code, out = _run(capsys, ["add", "--project", str(project), "--json-file", str(path)])
    assert code == 0
    assert out["counts"]["added"] == 1


@pytest.mark.parametrize("body", ["{}", '{"notes": "nope"}', "[]", "[1, 2]"])
def test_add_json_file_rejects_a_bad_shape(project, tmp_path, body):
    path = tmp_path / "approved.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["add", "--project", str(project), "--json-file", str(path)])
    assert json.loads(str(exc.value))["status"] == "error"


def test_add_json_file_must_exist(project, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["add", "--project", str(project), "--json-file", str(tmp_path / "nope.json")])
    assert "not found" in json.loads(str(exc.value))["error"]


# --- scan-prepare ---------------------------------------------------------

def test_scan_prepare_requires_a_profile_file(project):
    with pytest.raises(SystemExit) as exc:
        cli.main(["scan-prepare", "--project", str(project)])
    # argparse's own required-argument exit, not our JSON envelope.
    assert exc.value.code == 2


def test_scan_prepare_reports_a_missing_profile_as_json(project, profile_file, capsys):
    code, out = _run(
        capsys,
        [
            "scan-prepare",
            "--project",
            str(project),
            "--profile-file",
            str(profile_file.parent / "absent.md"),
        ],
    )
    assert code == 1
    assert out["status"] == "error"


def test_scan_prepare_takes_a_chapter_range(project, profile_file, capsys):
    code, out = _run(
        capsys,
        [
            "scan-prepare",
            "--project",
            str(project),
            "--profile-file",
            str(profile_file),
            "--chapters",
            "2",
        ],
    )
    assert code == 0
    assert out["chapters"] == ["chapter_02"]


# --- parser surface -------------------------------------------------------

def test_every_subcommand_is_dispatchable():
    parser = cli.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert set(actions[0].choices) == set(cli._DISPATCH)


def test_no_subcommand_is_an_argparse_error():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
