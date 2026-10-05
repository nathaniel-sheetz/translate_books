"""image-pass: inventory, jobs, the Codex seam, and the writes into ``images/``.

No test here launches Codex. ``generate`` takes the same ``runner`` / ``prober``
seams ``run_headless_wave`` does, so a fake runner stands in for ``codex exec``
(it drops a file where the real one would) and a fake prober stands in for
``codex login status``.

The two properties worth the most tests are the ones a published book depends
on: a refused login runs **zero** jobs, and an original is never lost.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from scripts import image_pass as cli
from src.image_pass import apply as ip_apply
from src.image_pass import backfill as ip_backfill
from src.image_pass import image_key, is_safe_key, jobs as ip_jobs, ledger
from src.image_pass import inventory as ip_inventory
from src.image_pass import report as ip_report

SOURCE = """THE POINTS OF THE COMPASS

[IMAGE:images/map.jpg:A MAP OF THE SCHOOL-ROOM.]

Some text.

[IMAGE:images/compass.png]

[IMAGE:images/gone.jpg:NOT ON DISK]
"""


def _image(path: Path, size=(300, 200), color=(200, 30, 30), fmt=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format=fmt)
    return path


@pytest.fixture(autouse=True)
def _isolated_codex_home(tmp_path, monkeypatch):
    """Harvesting looks under ``$CODEX_HOME``; never let it see the real one."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "projects" / "book"
    (root / "chapters").mkdir(parents=True)
    (root / "source.txt").write_text(SOURCE, encoding="utf-8")
    (root / "chapters" / "chapter_01.txt").write_text(
        "[IMAGE:images/map.jpg:UN MAPA DEL SALÓN.]\n", encoding="utf-8"
    )
    (root / "chapters" / "chapter_02.txt").write_text(
        "[IMAGE:images/compass.png]\n", encoding="utf-8"
    )
    (root / "project.json").write_text(json.dumps({"title": "Home Geography"}), encoding="utf-8")
    _image(root / "images" / "map.jpg", (300, 200))
    _image(root / "images" / "compass.png", (120, 120))
    _image(root / "images" / "stray.jpg", (50, 50))
    return root


TRANSLATE_JOB = {
    "image": "images/map.jpg",
    "mode": "translate",
    "instruction": "Keep the hand-lettered look.",
    "labels": {"NORTH": "NORTE", "SCHOOL-ROOM": "SALÓN DE CLASE"},
}


class FakeCodex:
    """Stands in for ``codex exec``: records each call, leaves a file behind.

    ``where="thread"`` (the default) is what codex-cli 0.157.0 really does: the
    stream opens with ``thread.started`` and the built-in tool writes
    ``$CODEX_HOME/generated_images/<thread id>/exec-<n>.png`` — ``count`` of
    them when the model calls the tool more than once.
    """

    def __init__(self, size=(1536, 1024), color=(10, 120, 60), rc=0, stdout="",
                 where="thread", count=1):
        self.size, self.color, self.rc, self.stdout = size, color, rc, stdout
        self.where, self.count = where, count
        self.calls: list[dict] = []

    def __call__(self, cmd, *, input_text, cwd):
        self.calls.append({"cmd": list(cmd), "prompt": input_text, "cwd": Path(cwd)})
        if self.rc != 0:
            return self.rc, self.stdout, "boom"
        generated = Path(os.environ["CODEX_HOME"]) / "generated_images"
        if self.where == "thread":
            thread_id = f"thread-{len(self.calls):02d}"
            for n in range(self.count):
                # Earlier attempts get a different colour and an older mtime, so
                # a harvest that takes the wrong one is visible.
                last = n == self.count - 1
                path = _image(
                    generated / thread_id / f"exec-{n}.png",
                    self.size, self.color if last else (0, 0, 0), fmt="PNG",
                )
                stamp = time.time() - (self.count - n) * 10
                os.utime(path, (stamp, stamp))
            event = {"type": "thread.started", "thread_id": thread_id}
            return 0, json.dumps(event) + chr(10) + self.stdout, ""
        if self.where == "output":
            _image(Path(cwd) / "output.png", self.size, self.color, fmt="PNG")
            return 0, self.stdout, ""
        if self.where == "saved_path":
            saved = _image(generated / "s1" / "ig_1.png", self.size, self.color, fmt="PNG")
            event = {"type": "item.completed", "item": {"type": "image_generation", "saved_path": str(saved)}}
            return 0, json.dumps(event) + chr(10), ""
        return 0, self.stdout, ""  # "nothing": exits 0, writes no file


def _prober(text: str, rc: int = 0):
    def probe(argv, *, env, cwd, timeout):
        probe.calls.append(list(argv))
        return rc, "", text

    probe.calls = []
    return probe


def _prepared(project: Path, *jobs) -> None:
    out = ip_jobs.prepare(project, list(jobs) or [TRANSLATE_JOB])
    assert out["status"] == "ok", out


# --- keys ------------------------------------------------------------------

@pytest.mark.parametrize(
    "ref,expected",
    [
        ("images/001.jpg", "001.jpg"),
        ("001.jpg", "001.jpg"),
        ("images\\maps\\a.png", "maps/a.png"),
        ("./images/001.jpg", "001.jpg"),
    ],
)
def test_image_key_accepts_both_spellings(ref, expected):
    assert image_key(ref) == expected


@pytest.mark.parametrize("key", ["", "../secret.png", "a/../../b.png", "C:/x.png", "a//b.png"])
def test_unsafe_keys_are_rejected(key):
    assert not is_safe_key(key)


# --- inventory -------------------------------------------------------------

def test_inventory_reads_tokens_not_the_directory(project: Path):
    out = ip_inventory.inventory(project)
    assert out["counts"]["referenced"] == 3
    assert out["missing"] == ["gone.jpg"]
    assert out["unreferenced"] == ["stray.jpg"]
    assert out["cover"] is None and out["counts"]["has_cover"] is False

    rows = {r["image"]: r for r in json.loads(Path(out["inventory_path"]).read_text("utf-8"))["images"]}
    assert rows["map.jpg"]["alt"] == "A MAP OF THE SCHOOL-ROOM."
    assert (rows["map.jpg"]["width"], rows["map.jpg"]["height"]) == (300, 200)
    assert rows["map.jpg"]["chapter"] == "chapter_01"
    assert rows["compass.png"]["chapter"] == "chapter_02" and rows["compass.png"]["alt"] is None
    assert rows["gone.jpg"]["missing"] is True
    # The cover is listed even when absent, so a cover job has a row to target.
    assert rows["cover.jpg"]["role"] == "cover" and rows["cover.jpg"]["missing"] is True


def test_inventory_finds_an_existing_cover(project: Path):
    _image(project / "images" / "cover.png", (600, 900))
    out = ip_inventory.inventory(project)
    assert out["cover"] == "cover.png"
    assert "cover.png" not in out["unreferenced"]


# --- prepare ---------------------------------------------------------------

def test_prepare_renders_the_approved_label_map(project: Path):
    out = ip_jobs.prepare(project, [TRANSLATE_JOB])
    assert out["status"] == "ok"
    assert out["counts"]["candidates_to_generate"] == 1
    prompt = Path(out["prepared"][0]["prompt_path"]).read_text("utf-8")
    assert prompt.startswith("$imagegen")
    assert '- "SCHOOL-ROOM" → "SALÓN DE CLASE"' in prompt
    assert "Keep the hand-lettered look." in prompt
    assert "Home Geography" in prompt and "Spanish" in prompt
    assert "`input.jpg`" in prompt and "300 wide by 200 high" in prompt
    # Learned on the first live run: asked to save a file, the model could not
    # and regenerated four times. The contract is one call and hands off.
    assert "exactly once" in prompt and "output.png" not in prompt
    assert "{{" not in prompt


def test_prepare_never_treats_the_instruction_as_a_template(project: Path):
    job = {**TRANSLATE_JOB, "instruction": "Leave the {{title}} cartouche alone."}
    out = ip_jobs.prepare(project, [job])
    prompt = Path(out["prepared"][0]["prompt_path"]).read_text("utf-8")
    assert "Leave the {{title}} cartouche alone." in prompt


@pytest.mark.parametrize(
    "job,needle",
    [
        ({**TRANSLATE_JOB, "labels": {}}, "labels map"),
        ({**TRANSLATE_JOB, "mode": "colorize"}, "is not one of"),
        ({**TRANSLATE_JOB, "instruction": "  "}, "instruction is required"),
        ({**TRANSLATE_JOB, "image": "nope.jpg"}, "no such file"),
        ({**TRANSLATE_JOB, "image": "../source.txt"}, "not a path inside images/"),
        ({**TRANSLATE_JOB, "candidates": 9}, "candidates must be"),
        ({**TRANSLATE_JOB, "label": {"A": "B"}}, "unknown key"),
        ({"image": "map.jpg", "mode": "cover", "instruction": "x"}, "must target one of"),
    ],
)
def test_prepare_refuses_a_bad_job(project: Path, job, needle):
    out = ip_jobs.prepare(project, [job])
    assert out["status"] == "error"
    assert needle in json.dumps(out["invalid"], ensure_ascii=False)


def test_prepare_is_all_or_nothing(project: Path):
    out = ip_jobs.prepare(project, [TRANSLATE_JOB, {**TRANSLATE_JOB, "image": "nope.jpg"}])
    assert out["status"] == "error" and out["counts"]["prepared"] == 0
    assert not ip_jobs.manifest_path(project).exists()
    assert not (ip_jobs.jobs_dir(project) / "map.jpg").exists()


def test_prepare_refuses_the_same_image_twice(project: Path):
    out = ip_jobs.prepare(project, [TRANSLATE_JOB, {**TRANSLATE_JOB, "image": "map.jpg"}])
    assert out["status"] == "error"
    assert "named twice" in out["invalid"][0]["problems"][0]


def test_prepare_merges_by_image_unless_told_to_replace(project: Path):
    _prepared(project)
    restore = {"image": "compass.png", "mode": "restore", "instruction": "Remove the foxing."}
    out = ip_jobs.prepare(project, [restore])
    assert out["counts"]["jobs_in_manifest"] == 2
    out = ip_jobs.prepare(project, [restore], replace=True)
    assert out["counts"]["jobs_in_manifest"] == 1


def test_a_cover_job_needs_no_existing_image(project: Path):
    out = ip_jobs.prepare(
        project,
        [{"image": "cover.jpg", "mode": "cover", "instruction": "A globe on a desk.",
          "labels": {"title": "Geografía del hogar"}}],
    )
    assert out["status"] == "ok"
    prompt = Path(out["prepared"][0]["prompt_path"]).read_text("utf-8")
    assert "Create the front cover" in prompt and "Geografía del hogar" in prompt
    assert "input." not in prompt


def test_a_cover_can_be_drawn_from_one_of_the_books_images(project: Path):
    job = {"image": "cover.jpg", "mode": "cover", "reference": "images/map.jpg",
           "instruction": "Portrait crop of the left side, hand-tinted."}
    out = ip_jobs.prepare(project, [job])
    assert out["status"] == "ok", out
    assert out["prepared"][0]["input_from"] == "reference"
    prompt = Path(out["prepared"][0]["prompt_path"]).read_text("utf-8")
    assert "from the attached image, `reference.jpg`" in prompt
    # The reference is landscape; the cover must not be told to keep its shape.
    assert "Keep the original's proportions" not in prompt
    assert "not the attached image's (300 wide by 200 high)" in prompt

    codex = FakeCodex(size=(1024, 1536))
    assert ip_jobs.generate(project, runner=codex)["counts"]["wrote"] == 1
    cmd = codex.calls[0]["cmd"]
    assert cmd[2] == "-i" and Path(cmd[3]).name == "reference.jpg"
    assert Path(cmd[3]).read_bytes() == (project / "images" / "map.jpg").read_bytes()

    page = Path(ip_report.review(project)["review_path"]).read_text("utf-8")
    assert "Drawn from map.jpg" in page and "../../images/map.jpg" in page

    # It lands as a new file with nothing to back up, and the reference is untouched.
    before = ledger.sha256_file(project / "images" / "map.jpg")
    applied = ip_apply.apply(project, [{"image": "cover.jpg", "candidate": 1}])
    assert applied["applied"][0]["backup_action"] == "none (new file)"
    assert applied["warnings"] == []
    assert ledger.sha256_file(project / "images" / "map.jpg") == before

    # Drawing it from a different picture is a different job.
    again = ip_jobs.prepare(project, [{**job, "reference": "compass.png"}])
    assert again["prepared"][0]["archived"] == 1


@pytest.mark.parametrize(
    "job,needle",
    [
        ({"image": "map.jpg", "mode": "restore", "instruction": "x", "reference": "compass.png"},
         "reference is for a cover job"),
        ({"image": "cover.jpg", "mode": "cover", "instruction": "x", "reference": "nope.jpg"},
         "reference nope.jpg: no such file"),
        ({"image": "cover.jpg", "mode": "cover", "instruction": "x", "reference": "../x.jpg"},
         "is not a path inside images/"),
    ],
)
def test_prepare_refuses_a_bad_reference(project: Path, job, needle):
    out = ip_jobs.prepare(project, [job])
    assert out["status"] == "error"
    assert needle in " ".join(out["invalid"][0]["problems"])


def test_a_cover_job_must_not_shadow_the_existing_cover(project: Path):
    _image(project / "images" / "cover.png", (600, 900))
    out = ip_jobs.prepare(
        project, [{"image": "cover.jpg", "mode": "cover", "instruction": "x"}]
    )
    assert out["status"] == "error"
    assert "cover is cover.png" in out["invalid"][0]["problems"][0]


def test_reprepare_with_a_new_prompt_archives_candidates_instead_of_deleting(project: Path):
    _prepared(project)
    assert ip_jobs.generate(project, runner=FakeCodex())["counts"]["wrote"] == 1
    job_dir = ip_jobs.jobs_dir(project) / "map.jpg"

    # Same prompt: the candidate that cost plan usage is kept.
    out = ip_jobs.prepare(project, [TRANSLATE_JOB])
    assert out["prepared"][0]["have"] == 1 and out["counts"]["archived_candidates"] == 0

    out = ip_jobs.prepare(project, [{**TRANSLATE_JOB, "instruction": "Bolder lettering."}])
    assert out["counts"]["archived_candidates"] == 1
    assert out["counts"]["candidates_to_generate"] == 1
    assert not (job_dir / "cand_01.png").exists()
    assert len(list((job_dir / "previous").glob("*/cand_01.png"))) == 1


# --- generate --------------------------------------------------------------

def test_estimate_runs_nothing(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 3})
    codex = FakeCodex()
    out = ip_jobs.generate(project, estimate=True, runner=codex)
    assert codex.calls == []
    assert out["estimate"] is True
    assert out["plan"]["candidates"] == 3 and out["plan"]["images"] == 1
    assert out["plan"]["minutes_measured"] is False
    assert out["plan"]["estimated_minutes"] == 3 * ip_jobs.ASSUMED_MINUTES_PER_IMAGE
    assert "3-5x" in out["limit_warning"]


def test_generate_harvests_a_png_candidate_and_attaches_the_original(project: Path):
    _prepared(project)
    codex = FakeCodex()
    out = ip_jobs.generate(project, runner=codex)
    assert out["status"] == "ok" and out["counts"] == {"wrote": 1, "failed": 0, "not_run": 0, "todo": 1}
    wrote = out["wrote"][0]
    assert wrote["harvested_from"] == "codex_home:thread"
    assert "images_generated" not in wrote
    assert Path(wrote["path"]).name == "cand_01.png"
    with Image.open(wrote["path"]) as image:
        assert image.format == "PNG" and image.size == (1536, 1024)

    call = codex.calls[0]
    cmd = call["cmd"]
    assert cmd[1] == "exec"
    # -i is variadic: it must come first and nothing positional may follow it.
    assert cmd[2] == "-i" and Path(cmd[3]).name == "input.jpg"
    assert Path(cmd[3]).read_bytes() == (project / "images" / "map.jpg").read_bytes()
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert cmd[cmd.index("-c") + 1] == "model_provider=openai"
    assert cmd[cmd.index("-C") + 1] == str(call["cwd"])
    assert "--json" in cmd and "--ephemeral" in cmd and cmd[-1] != "-"
    assert call["prompt"].startswith("$imagegen")


def test_generate_takes_the_last_image_and_reports_the_extra_generations(project: Path):
    _prepared(project)
    out = ip_jobs.generate(project, runner=FakeCodex(count=4, color=(10, 120, 60)))
    wrote = out["wrote"][0]
    assert wrote["harvested_from"] == "codex_home:thread" and wrote["images_generated"] == 4
    with Image.open(wrote["path"]) as image:
        assert image.getpixel((0, 0)) == (10, 120, 60)


def test_parallel_jobs_each_harvest_their_own_thread(project: Path):
    restore = {"image": "compass.png", "mode": "restore", "instruction": "Clean it."}
    _prepared(project, TRANSLATE_JOB, restore)
    out = ip_jobs.generate(project, runner=FakeCodex(), concurrency=2)
    assert out["counts"]["wrote"] == 2
    assert {row["harvested_from"] for row in out["wrote"]} == {"codex_home:thread"}


@pytest.mark.parametrize(
    "where,expected",
    [("output", "run_dir"), ("saved_path", "event:saved_path")],
)
def test_generate_still_finds_an_image_left_elsewhere(project: Path, where, expected):
    _prepared(project)
    out = ip_jobs.generate(project, runner=FakeCodex(where=where))
    assert out["wrote"][0]["harvested_from"] == expected


def test_generate_only_fills_what_is_missing(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 2})
    codex = FakeCodex()
    assert ip_jobs.generate(project, runner=codex)["counts"]["wrote"] == 2
    out = ip_jobs.generate(project, runner=codex)
    assert out["counts"]["todo"] == 0 and len(codex.calls) == 2


def test_an_exit_zero_with_no_image_is_a_failure(project: Path):
    _prepared(project)
    out = ip_jobs.generate(project, runner=FakeCodex(where="nothing"))
    assert out["status"] == "error" and out["counts"]["failed"] == 1
    assert "left no image" in out["failed"][0]["error"]
    assert not (ip_jobs.jobs_dir(project) / "map.jpg" / "cand_01.png").exists()


def test_an_output_identical_to_the_input_is_not_a_candidate(project: Path):
    _prepared(project)

    def echo(cmd, *, input_text, cwd):
        (Path(cwd) / "output.png").write_bytes((Path(cwd) / "input.jpg").read_bytes())
        return 0, "", ""

    out = ip_jobs.generate(project, runner=echo)
    assert out["counts"]["failed"] == 1
    assert "byte-identical" in out["failed"][0]["error"]


def test_generate_refuses_before_any_job_on_an_api_key_login(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 2})
    codex = FakeCodex()
    for estimate in (True, False):
        out = ip_jobs.generate(
            project,
            estimate=estimate,
            runner=codex,
            cli_bin="python",  # anything on PATH; the prober is what answers
            prober=_prober("Logged in using an API key - sk-proj-***abcd"),
        )
        assert out["status"] == "error"
        assert "API key" in out["error"] and "sk-proj" not in out["error"]
        assert out["wrote"] == [] and out["failed"] == []
    assert codex.calls == []


def test_generate_runs_on_a_chatgpt_login_and_probes_every_job(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 2})
    prober = _prober("Logged in using ChatGPT")
    out = ip_jobs.generate(project, runner=FakeCodex(), cli_bin="python", prober=prober)
    assert out["counts"]["wrote"] == 2
    assert all(argv[1:] == ["login", "status"] for argv in prober.calls)
    assert len(prober.calls) == 3  # once up front, then once per job


def test_a_usage_limit_stops_the_batch_instead_of_burning_it(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 3})
    codex = FakeCodex(
        rc=1,
        stdout=json.dumps({"type": "turn.failed", "error": {"message": "usage_limit_reached"}}),
    )
    out = ip_jobs.generate(project, runner=codex)
    assert len(codex.calls) == 1
    assert out["status"] == "error"
    assert out["counts"]["failed"] == 1 and out["counts"]["not_run"] == 2
    assert "usage limit" in out["error"]


def test_a_rejected_model_stops_the_batch_and_says_how_to_fix_it(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 3})
    inner = json.dumps({"type": "error", "status": 400, "error": {
        "type": "invalid_request_error",
        "message": "The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account.",
    }})
    codex = FakeCodex(rc=1, stdout=json.dumps({"type": "turn.failed", "error": {"message": inner}}))
    out = ip_jobs.generate(project, runner=codex)
    assert len(codex.calls) == 1 and out["counts"]["not_run"] == 2
    assert out["failed"][0]["error"].startswith("The 'gpt-5.4' model is not supported")
    assert "--model" in out["error"]


def test_generate_rejects_an_unprepared_target(project: Path):
    _prepared(project)
    out = ip_jobs.generate(project, target_ids=["compass.png"], runner=FakeCodex())
    assert out["status"] == "error" and "not prepared" in out["error"]


def test_generate_measures_its_own_minutes_for_the_next_estimate(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 2})
    ip_jobs.generate(project, target_ids=["map.jpg"], runner=FakeCodex())
    rows = [json.loads(line) for line in ip_jobs.usage_path(project).read_text("utf-8").splitlines()]
    assert [row["candidate"] for row in rows] == [1, 2] and all(row["ok"] for row in rows)


# --- review ----------------------------------------------------------------

def test_review_page_links_original_and_candidates_by_relative_path(project: Path):
    _prepared(project)
    ip_jobs.generate(project, runner=FakeCodex(size=(1024, 1024)))
    out = ip_report.review(project)
    page = Path(out["review_path"]).read_text("utf-8")
    assert 'src="../../images/map.jpg"' in page
    assert 'src="jobs/map.jpg/cand_01.png"' in page
    assert "SALÓN DE CLASE" in page
    # 1:1 against a 3:2 original is flagged for the human, not hidden.
    assert out["jobs"][0]["candidates"][0]["flags"]
    assert "aspect" in page


# --- apply / revert / verify -----------------------------------------------

def _with_candidate(project: Path, **codex) -> None:
    _prepared(project)
    assert ip_jobs.generate(project, runner=FakeCodex(**codex))["counts"]["wrote"] == 1


def test_dry_run_writes_nothing(project: Path):
    _with_candidate(project)
    before = (project / "images" / "map.jpg").read_bytes()
    out = ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}], dry_run=True)
    assert out["status"] == "ok" and out["counts"]["planned"] == 1
    assert out["planned"][0]["backup_action"] == "created"
    assert (project / "images" / "map.jpg").read_bytes() == before
    assert not (project / "images_original").exists()
    assert out["ledger_path"] is None and not ledger.ledger_path(project).exists()


def test_apply_keeps_the_filename_and_format_and_backs_up_once(project: Path):
    _with_candidate(project)
    target = project / "images" / "map.jpg"
    original = target.read_bytes()

    out = ip_apply.apply(project, [{"image": "images/map.jpg", "candidate": 1}])
    assert out["status"] == "ok" and out["counts"]["applied"] == 1
    assert (project / "images_original" / "map.jpg").read_bytes() == original
    with Image.open(target) as image:
        assert image.format == "JPEG"
        # Downscaled to the floor rather than shipped at 1536 px for a 300 px slot.
        assert max(image.size) == 1024
    assert sorted(p.name for p in (project / "images").iterdir()) == [
        "compass.png", "map.jpg", "stray.jpg",
    ]
    assert (project / "source.txt").read_text("utf-8") == SOURCE

    # A second replacement must not turn the generated picture into "the original".
    ip_jobs.prepare(project, [{**TRANSLATE_JOB, "instruction": "Bolder."}])
    ip_jobs.generate(project, runner=FakeCodex(color=(0, 0, 200)))
    out = ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    assert out["applied"][0]["backup_action"] == "kept (already backed up)"
    assert (project / "images_original" / "map.jpg").read_bytes() == original

    rows = ledger.read_rows(project)
    assert [row["action"] for row in rows] == ["apply", "apply"]
    assert rows[0]["labels"] == TRANSLATE_JOB["labels"]
    assert rows[0]["sha256_after"] == rows[1]["sha256_before"]


def test_a_redo_works_from_the_original_not_the_replacement(project: Path):
    _with_candidate(project)
    original = (project / "images" / "map.jpg").read_bytes()
    ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    ip_jobs.prepare(project, [{**TRANSLATE_JOB, "instruction": "Again."}])
    codex = FakeCodex()
    ip_jobs.generate(project, runner=codex)
    assert Path(codex.calls[0]["cmd"][3]).read_bytes() == original


def test_apply_lands_the_good_rows_and_names_the_bad_one(project: Path):
    _with_candidate(project)
    out = ip_apply.apply(
        project,
        [
            {"image": "map.jpg", "candidate": 1},
            {"image": "compass.png", "candidate": 1},
            {"image": "stray.jpg"},
        ],
    )
    assert out["status"] == "partial"
    assert out["counts"]["applied"] == 1
    assert out["refused"][0]["image"] == "compass.png"
    assert "no prepared job" in out["refused"][0]["problems"][0]
    assert out["counts"]["invalid"] == 1


def test_apply_refuses_a_candidate_that_was_never_generated(project: Path):
    _with_candidate(project)
    out = ip_apply.apply(project, [{"image": "map.jpg", "candidate": 2}])
    assert out["status"] == "error"
    assert "candidate 2 does not exist (have: [1])" in out["refused"][0]["problems"][0]
    assert not (project / "images_original").exists()


def test_apply_warns_when_the_proportions_changed(project: Path):
    _with_candidate(project, size=(1024, 1024))
    out = ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    assert out["status"] == "ok" and out["warnings"][0]["code"] == "aspect_changed"
    assert ip_apply.verify(project)["by_code"] == {"aspect_changed": 1, "missing_file": 1}


def test_skip_and_redo_are_recorded_without_writing(project: Path):
    _with_candidate(project)
    before = (project / "images" / "map.jpg").read_bytes()
    out = ip_apply.apply(
        project, [{"image": "map.jpg", "verdict": "redo", "note": "NORTE is misspelled"}]
    )
    assert out["status"] == "ok" and out["counts"]["redo"] == 1
    assert (project / "images" / "map.jpg").read_bytes() == before
    row = ledger.read_rows(project)[0]
    assert row["action"] == "redo" and row["note"] == "NORTE is misspelled"
    assert ledger.current_state(project) == {}


def test_revert_restores_the_original_byte_for_byte(project: Path):
    _with_candidate(project)
    target = project / "images" / "map.jpg"
    original = target.read_bytes()
    ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    assert target.read_bytes() != original

    out = ip_apply.revert(project, ["all"])
    assert out["status"] == "ok" and out["reverted"] == [{"image": "map.jpg", "action": "restored"}]
    assert target.read_bytes() == original
    # The backup stays: a later apply must still find the real original.
    assert (project / "images_original" / "map.jpg").read_bytes() == original
    assert ledger.status_of(ledger.current_state(project)["map.jpg"]) == ledger.STATUS_REVERTED
    assert ip_apply.revert(project, ["all"])["counts"]["reverted"] == 0


def test_revert_refuses_an_image_with_no_backup(project: Path):
    out = ip_apply.revert(project, ["compass.png"])
    assert out["status"] == "error"
    assert "no backup" in out["refused"][0]["problems"][0]


def test_a_new_cover_lands_and_reverts_to_absent(project: Path):
    ip_jobs.prepare(project, [{"image": "cover.jpg", "mode": "cover", "instruction": "A globe."}])
    codex = FakeCodex(size=(1024, 1536))
    ip_jobs.generate(project, runner=codex)
    assert "-i" not in codex.calls[0]["cmd"]

    out = ip_apply.apply(project, [{"image": "cover.jpg", "candidate": 1}])
    cover = project / "images" / "cover.jpg"
    assert out["applied"][0]["backup_action"] == "none (new file)"
    with Image.open(cover) as image:
        assert image.format == "JPEG" and image.size == (1024, 1536)
    assert not (project / "images_original" / "cover.jpg").exists()
    assert ledger.read_rows(project)[-1]["created"] is True

    assert ip_apply.revert(project, ["cover.jpg"])["reverted"][0]["action"].startswith("removed")
    assert not cover.exists()


def test_revert_will_not_delete_a_created_file_that_changed(project: Path):
    ip_jobs.prepare(project, [{"image": "cover.jpg", "mode": "cover", "instruction": "A globe."}])
    ip_jobs.generate(project, runner=FakeCodex(size=(1024, 1536)))
    ip_apply.apply(project, [{"image": "cover.jpg", "candidate": 1}])
    _image(project / "images" / "cover.jpg", (10, 15))
    out = ip_apply.revert(project, ["cover.jpg"])
    assert out["status"] == "error" and (project / "images" / "cover.jpg").exists()


def test_verify_names_what_a_reader_would_see(project: Path):
    out = ip_apply.verify(project)
    assert out["status"] == "broken"
    assert out["broken"] == [{
        "image": "gone.jpg",
        "code": "missing_file",
        "detail": "[IMAGE:images/gone.jpg] names a file that is not in images/",
    }]


def test_verify_catches_a_lost_backup(project: Path):
    _with_candidate(project)
    ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    (project / "images_original" / "map.jpg").unlink()
    assert ip_apply.verify(project)["by_code"]["no_backup"] == 1


def test_verify_is_clean_after_apply_and_after_revert(project: Path):
    (project / "source.txt").write_text(SOURCE.replace("[IMAGE:images/gone.jpg:NOT ON DISK]", ""), "utf-8")
    _with_candidate(project)
    ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    out = ip_apply.verify(project)
    assert out["status"] == "ok" and out["warned"] == [] and out["counts"]["replaced"] == 1
    ip_apply.revert(project, ["map.jpg"])
    out = ip_apply.verify(project)
    assert out["status"] == "ok" and out["warned"] == [] and out["counts"]["replaced"] == 0


# --- CLI envelope ----------------------------------------------------------

def _run(capsys, argv):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_cli_prints_one_json_object_and_mirrors_it(project: Path, capsys, tmp_path: Path):
    code, out, err = _run(capsys, ["inventory", "--project", str(project)])
    assert code == 0 and out["counts"]["referenced"] == 3
    sidecar = project / ".harness" / "images" / "last_output.json"
    assert f"OUTPUT_JSON: {sidecar}" in err
    assert json.loads(sidecar.read_text("utf-8"))["counts"] == out["counts"]

    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps({"jobs": [TRANSLATE_JOB]}, ensure_ascii=False), "utf-8")
    code, out, _ = _run(capsys, ["prepare", "--project", str(project), "--json-file", str(jobs_file)])
    assert code == 0 and out["counts"]["prepared"] == 1

    code, out, _ = _run(capsys, ["verify", "--project", str(project)])
    assert code == 1 and out["status"] == "broken"


def test_cli_dies_with_json_on_a_missing_project(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["inventory", "--project", "no-such-book-xyz"])
    assert json.loads(str(exc.value))["status"] == "error"


# --- backfill: the publisher's larger scans ----------------------------------

class Scans:
    """Stands in for the source site: larger scans by URL, and a fetch counter."""

    def __init__(self, tmp_path: Path, **scans):
        self.calls: list[str] = []
        self.bytes: dict[str, bytes] = {}
        self.links: list[dict] = []
        for inline, (name, size) in scans.items():
            inline = inline.replace("__", ".")
            path = _image(tmp_path / "site" / name, size, (20, 20, 20))
            url = f"https://pg.example/images/{name}"
            self.bytes[url] = path.read_bytes()
            self.links.append({"url": url, "name": name, "inline_names": [inline]})

    def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        return self.bytes[url]


def _size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def test_backfill_puts_the_larger_scan_behind_the_old_name(project: Path, tmp_path: Path):
    scans = Scans(tmp_path, map__jpg=("map_l.gif", (900, 600)), gone__jpg=("gone_l.gif", (400, 300)))
    out = ip_backfill.backfill(project, scans.links, fetch=scans.fetch)
    assert out["status"] == "ok" and out["counts"]["upgraded"] == 2, out
    assert out["unmatched"] == ["compass.png"]

    target = project / "images" / "map.jpg"
    with Image.open(target) as image:
        assert image.format == "JPEG" and image.size == (900, 600)
    # A token whose file was missing gets one; nothing is called a replacement,
    # and no backup of the thumbnail is kept.
    assert _size(project / "images" / "gone.jpg") == (400, 300)
    assert not (project / "images_original").exists()
    assert ledger.current_state(project) == {}
    row = [r for r in ledger.read_rows(project) if r["image"] == "map.jpg"][0]
    assert row["action"] == "backfill" and row["baseline"] == "images"
    assert row["size_before"] == [300, 200] and row["size_after"] == [900, 600]
    assert ip_apply.verify(project)["status"] == "ok"


def test_backfill_dry_run_writes_nothing_and_a_second_run_changes_nothing(
    project: Path, tmp_path: Path
):
    scans = Scans(tmp_path, map__jpg=("map_l.gif", (900, 600)))
    before = ledger.sha256_file(project / "images" / "map.jpg")
    out = ip_backfill.backfill(project, scans.links, fetch=scans.fetch, dry_run=True)
    assert out["planned"][0]["from_size"] == [300, 200]
    assert out["planned"][0]["to_size"] == [900, 600]
    assert out["planned"][0]["writes"] == ["images/map.jpg"]
    assert ledger.sha256_file(project / "images" / "map.jpg") == before
    assert not ledger.ledger_path(project).exists()

    assert ip_backfill.backfill(project, scans.links, fetch=scans.fetch)["counts"]["upgraded"] == 1
    again = ip_backfill.backfill(project, scans.links, fetch=scans.fetch)
    assert again["counts"]["upgraded"] == 0
    assert again["skipped"][0]["reason"] == "not larger"
    assert len(scans.calls) == 1  # the dry run's fetch served all three runs


def test_backfill_copies_a_scan_already_in_the_right_format(project: Path, tmp_path: Path):
    scans = Scans(tmp_path, compass__png=("compass_l.png", (480, 480)))
    ip_backfill.backfill(project, scans.links, fetch=scans.fetch)
    assert (project / "images" / "compass.png").read_bytes() == scans.bytes[scans.links[0]["url"]]


def test_backfill_upgrades_the_original_of_a_replaced_image_and_leaves_the_replacement(
    project: Path, tmp_path: Path
):
    _with_candidate(project)
    ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    replacement = ledger.sha256_file(project / "images" / "map.jpg")

    scans = Scans(tmp_path, map__jpg=("map_l.gif", (900, 600)))
    out = ip_backfill.backfill(project, scans.links, fetch=scans.fetch)
    row = out["upgraded"][0]
    assert row["writes"] == ["images_original/map.jpg"] and "left_alone" in row
    assert out["stale_jobs"] == ["map.jpg"]
    assert ledger.sha256_file(project / "images" / "map.jpg") == replacement
    assert _size(project / "images_original" / "map.jpg") == (900, 600)
    # Still a replacement, and the upgraded backup is the original verify expects.
    assert ledger.status_of(ledger.current_state(project)["map.jpg"]) == ledger.STATUS_REPLACED
    verdict = ip_apply.verify(project)
    assert "backup_changed" not in verdict["by_code"], verdict

    # The job now starts from the larger scan, so the candidate drawn from the
    # thumbnail is set aside rather than kept as its answer.
    again = ip_jobs.prepare(project, [TRANSLATE_JOB])
    assert again["prepared"][0]["archived"] == 1 and again["prepared"][0]["have"] == 0
    assert ip_jobs.load_manifest(project)["jobs"][0]["width"] == 900

    ip_apply.revert(project, ["map.jpg"])
    assert _size(project / "images" / "map.jpg") == (900, 600)


def test_backfill_upgrades_both_copies_of_a_reverted_image(project: Path, tmp_path: Path):
    _with_candidate(project)
    ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    ip_apply.revert(project, ["map.jpg"])

    scans = Scans(tmp_path, map__jpg=("map_l.gif", (900, 600)))
    out = ip_backfill.backfill(project, scans.links, fetch=scans.fetch)
    assert out["upgraded"][0]["writes"] == ["images_original/map.jpg", "images/map.jpg"]
    assert (project / "images" / "map.jpg").read_bytes() == \
        (project / "images_original" / "map.jpg").read_bytes()
    assert _size(project / "images" / "map.jpg") == (900, 600)
    verdict = ip_apply.verify(project)
    assert not {"backup_changed", "reverted_but_differs"} & set(verdict["by_code"]), verdict


def test_backfill_reports_split_halves_and_never_joins_them(project: Path, tmp_path: Path):
    whole = _image(tmp_path / "site" / "whole_l.gif", (900, 600))
    link = {
        "url": "https://pg.example/images/whole_l.gif",
        "name": "whole_l.gif",
        "inline_names": ["map.jpg", "compass.png"],
    }
    before = ledger.sha256_file(project / "images" / "map.jpg")
    out = ip_backfill.backfill(project, [link], fetch=lambda url: whole.read_bytes())
    assert [row["image"] for row in out["split"]] == ["map.jpg", "compass.png"]
    assert out["counts"]["upgraded"] == 0
    assert ledger.sha256_file(project / "images" / "map.jpg") == before


def test_backfill_lands_what_it_can_and_names_what_it_could_not(project: Path, tmp_path: Path):
    scans = Scans(
        tmp_path, map__jpg=("map_l.gif", (900, 600)), compass__png=("compass_l.png", (480, 480))
    )

    def fetch(url: str) -> bytes:
        if "compass" in url:
            raise OSError("404 Not Found")
        return scans.fetch(url)

    out = ip_backfill.backfill(project, scans.links, fetch=fetch, only=["map.jpg", "compass.png", "nope.jpg"])
    assert out["status"] == "partial" and out["counts"]["upgraded"] == 1
    assert {row["image"]: row["error"] for row in out["failed"]} == {
        "nope.jpg": "not an image this book references",
        "compass.png": "could not fetch: 404 Not Found",
    }
    assert _size(project / "images" / "compass.png") == (120, 120)


def _drawing(path: Path, size, seed: int) -> Path:
    """A picture with something in it: the same seed is the same picture at any size."""
    import random

    from PIL import ImageDraw

    rng = random.Random(seed)
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        x0, y0 = rng.random() * 0.8, rng.random() * 0.8
        x1, y1 = x0 + 0.1 + rng.random() * 0.2, y0 + 0.1 + rng.random() * 0.2
        draw.rectangle(
            [x0 * size[0], y0 * size[1], x1 * size[0], y1 * size[1]], fill=rng.randrange(0, 200)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(path)
    return path


def _link(tmp_path: Path, inline: str, name: str, size, seed: int) -> tuple[dict, bytes]:
    data = _drawing(tmp_path / "site" / name, size, seed).read_bytes()
    return {"url": f"https://pg.example/images/{name}", "name": name, "inline_names": [inline]}, data


def test_picture_similarity_knows_the_same_picture_at_another_size(tmp_path: Path):
    small = _drawing(tmp_path / "a.jpg", (240, 160), seed=1)
    large = _drawing(tmp_path / "a_l.gif", (900, 600), seed=1)
    other = _drawing(tmp_path / "b_l.gif", (900, 600), seed=2)
    assert ip_inventory.picture_similarity(small, large) >= ip_inventory.SAME_PICTURE
    assert ip_inventory.picture_similarity(small, other) < ip_inventory.SAME_PICTURE
    assert ip_inventory.picture_similarity(small.read_bytes(), large) >= ip_inventory.SAME_PICTURE
    # A blank image correlates with nothing; an unreadable one cannot be measured.
    assert ip_inventory.picture_similarity(_image(tmp_path / "flat.png"), large) is None
    assert ip_inventory.picture_similarity(b"not an image", large) is None


def test_backfill_leaves_an_image_whose_linked_scan_is_another_picture(
    project: Path, tmp_path: Path
):
    _drawing(project / "images" / "map.jpg", (300, 200), seed=1)
    before = ledger.sha256_file(project / "images" / "map.jpg")
    link, data = _link(tmp_path, "map.jpg", "map_l.gif", (900, 600), seed=2)

    out = ip_backfill.backfill(project, [link], fetch=lambda url: data, dry_run=True)
    assert out["planned"] == [] and out["unlike"][0]["image"] == "map.jpg"
    out = ip_backfill.backfill(project, [link], fetch=lambda url: data)
    assert out["counts"]["upgraded"] == 0 and out["unlike"][0]["score"] < 0.95
    assert "--accept" in out["instructions"]
    assert ledger.sha256_file(project / "images" / "map.jpg") == before

    # Someone looked and says it is the same plate, re-cropped.
    out = ip_backfill.backfill(project, [link], fetch=lambda url: data, accept=["images/map.jpg"])
    assert out["upgraded"][0]["accepted"] is True
    assert _size(project / "images" / "map.jpg") == (900, 600)


def test_backfill_finds_the_right_scan_when_the_page_crosses_its_links(
    project: Path, tmp_path: Path
):
    _drawing(project / "images" / "map.jpg", (300, 200), seed=1)
    _drawing(project / "images" / "compass.png", (120, 120), seed=2)
    # The page links each thumbnail to the other one's scan.
    for_map, compass_scan = _link(tmp_path, "map.jpg", "map_l.gif", (480, 480), seed=2)
    for_compass, map_scan = _link(tmp_path, "compass.png", "compass_l.gif", (900, 600), seed=1)
    served = {for_map["url"]: compass_scan, for_compass["url"]: map_scan}

    out = ip_backfill.backfill(project, [for_map, for_compass], fetch=served.__getitem__)
    assert out["counts"] == {**out["counts"], "upgraded": 2, "relinked": 2, "unlike": 0}
    rows = {row["image"]: row for row in out["upgraded"]}
    assert rows["map.jpg"]["url"] == for_compass["url"]
    assert rows["map.jpg"]["relinked_from"] == for_map["url"]
    assert _size(project / "images" / "map.jpg") == (900, 600)
    assert _size(project / "images" / "compass.png") == (480, 480)
    assert ip_inventory.picture_similarity(
        project / "images" / "map.jpg", tmp_path / "site" / "compass_l.gif"
    ) >= ip_inventory.SAME_PICTURE
    # And it stays put: the crossed links do not un-fix it on the next run.
    again = ip_backfill.backfill(project, [for_map, for_compass], fetch=served.__getitem__)
    assert again["counts"]["upgraded"] == 0 and again["counts"]["skipped"] == 2


def test_cli_backfill_reads_a_saved_page_and_refuses_without_a_source(
    project: Path, capsys, tmp_path: Path
):
    with pytest.raises(SystemExit) as exc:
        cli.main(["backfill", "--project", str(project)])
    assert "--source" in json.loads(str(exc.value))["error"]

    site = tmp_path / "site"
    _image(site / "images" / "map_l.gif", (900, 600))
    page = site / "book.html"
    page.write_text(
        '<html><body><a href="images/map_l.gif"><img src="images/map.jpg" alt="A MAP"></a>'
        "</body></html>",
        encoding="utf-8",
    )
    code, out, _ = _run(capsys, ["backfill", "--project", str(project), "--source", str(page)])
    assert code == 0 and out["counts"]["upgraded"] == 1, out
    assert _size(project / "images" / "map.jpg") == (900, 600)

    # The URL an ingest recorded is the default source.
    (project / "project.json").write_text(json.dumps({"gutenberg_url": str(page)}), "utf-8")
    code, out, _ = _run(capsys, ["backfill", "--project", str(project), "--dry-run"])
    assert code == 0 and out["source"] == str(page) and out["skipped"][0]["image"] == "map.jpg"
