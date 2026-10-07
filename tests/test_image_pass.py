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
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from scripts import image_pass as cli
from src.image_pass import apply as ip_apply
from src.image_pass import backfill as ip_backfill
from src.image_pass import image_key, is_safe_key, jobs as ip_jobs, ledger
from src.image_pass import inventory as ip_inventory
from src.image_pass import board as ip_board
from src.image_pass import composite as ip_composite
from src.harness import locks
from src.image_pass import feedback as ip_feedback

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
        self._lock = threading.Lock()

    def __call__(self, cmd, *, input_text, cwd):
        # Ten of these run at once now: number the call under a lock, or two
        # workers read the same length and share a thread folder.
        with self._lock:
            self.calls.append({"cmd": list(cmd), "prompt": input_text, "cwd": Path(cwd)})
            number = len(self.calls)
        rc = self.rc(cmd) if callable(self.rc) else self.rc
        if rc != 0:
            return rc, self.stdout, "boom"
        generated = Path(os.environ["CODEX_HOME"]) / "generated_images"
        if self.where == "thread":
            thread_id = f"thread-{number:02d}"
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


def _board_row(project: Path, image: str) -> dict:
    return next(row for row in ip_board.build(project)["images"] if row["image"] == image)


def _stamp(seconds: int) -> str:
    """A ledger-style timestamp ``seconds`` from now. Stamps are whole seconds,
    so a test that needs "later" has to say so rather than race the clock."""
    moment = datetime.now().astimezone() + timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds")


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


def test_prepare_will_not_merge_onto_a_manifest_it_cannot_read(project: Path):
    _prepared(
        project,
        TRANSLATE_JOB,
        {"image": "compass.png", "mode": "restore", "instruction": "Remove the foxing."},
    )
    manifest = ip_jobs.manifest_path(project)
    torn = manifest.read_text("utf-8")[:40]  # what a kill mid-write used to leave
    manifest.write_text(torn, encoding="utf-8")
    out = ip_jobs.prepare(project, [TRANSLATE_JOB])
    assert out["status"] == "error" and "cannot be read" in out["error"]
    assert manifest.read_text("utf-8") == torn
    # Told the batch is the whole manifest, there is nothing to lose by writing it.
    out = ip_jobs.prepare(project, [TRANSLATE_JOB], replace=True)
    assert out["status"] == "ok" and out["counts"]["jobs_in_manifest"] == 1


def test_two_images_cannot_share_a_job_folder(project: Path):
    _image(project / "images" / "a" / "b.jpg")
    _image(project / "images" / "a_b.jpg")
    _prepared(project, {**TRANSLATE_JOB, "image": "a/b.jpg"})
    out = ip_jobs.prepare(project, [{**TRANSLATE_JOB, "image": "a_b.jpg"}])
    assert out["status"] == "error"
    assert "already belongs to a/b.jpg" in out["invalid"][0]["problems"][0]
    assert [job["image"] for job in ip_jobs.load_manifest(project)["jobs"]] == ["a/b.jpg"]


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

    # The board shows what the cover was drawn from beside the candidate.
    drawn = next(p for p in _board_row(project, "cover.jpg")["pictures"] if p["kind"] == "reference")
    assert (drawn["key"], drawn["source"]) == ("map.jpg", "images")

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
    out = ip_jobs.generate(project, estimate=True, concurrency=1, runner=codex)
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
    out = ip_jobs.generate(project, concurrency=1, runner=codex)
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
    out = ip_jobs.generate(project, concurrency=1, runner=codex)
    assert len(codex.calls) == 1 and out["counts"]["not_run"] == 2
    assert out["failed"][0]["error"].startswith("The 'gpt-5.4' model is not supported")
    assert "--model" in out["error"]


def test_generate_rejects_an_unprepared_target(project: Path):
    _prepared(project)
    out = ip_jobs.generate(project, target_ids=["compass.png"], runner=FakeCodex())
    assert out["status"] == "error" and "not prepared" in out["error"]


def test_generate_measures_its_own_minutes_for_the_next_estimate(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 2})
    ip_jobs.generate(project, target_ids=["map.jpg"], concurrency=1, runner=FakeCodex())
    rows = [json.loads(line) for line in ip_jobs.usage_path(project).read_text("utf-8").splitlines()]
    assert [row["candidate"] for row in rows] == [1, 2] and all(row["ok"] for row in rows)

# --- generate: several at once, on more than one model ----------------------

RESTORE_JOB = {"image": "compass.png", "mode": "restore", "instruction": "Clean it."}

_MODEL_REJECTED = json.dumps({"type": "turn.failed", "error": {"message": json.dumps({
    "type": "error", "status": 400, "error": {
        "type": "invalid_request_error",
        "message": "The 'gpt-old' model is not supported when using Codex with a ChatGPT account.",
    },
})}})


def _model_of(call: dict) -> str | None:
    cmd = call["cmd"]
    return cmd[cmd.index("-m") + 1] if "-m" in cmd else None


def _usage(project: Path) -> list[dict]:
    lines = ip_jobs.usage_path(project).read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _planned_models(project: Path, **kwargs) -> dict:
    out = ip_jobs.generate(project, estimate=True, runner=FakeCodex(), **kwargs)
    return {job["id"]: job["models"] for job in out["plan"]["jobs"]}


def test_two_models_on_one_job_are_two_candidates_of_one_run(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 2, "model": ["gpt-6-luna", "gpt-6-sol"]})
    codex = FakeCodex()
    said: list[str] = []
    out = ip_jobs.generate(project, runner=codex, progress=said.append)
    assert out["status"] == "ok" and len(codex.calls) == 2
    assert sorted(_model_of(call) for call in codex.calls) == ["gpt-6-luna", "gpt-6-sol"]
    assert out["plan"]["models"] == {"gpt-6-luna": 1, "gpt-6-sol": 1}
    assert {row["candidate"]: row["model"] for row in out["wrote"]} == {
        1: "gpt-6-luna", 2: "gpt-6-sol",
    }
    assert len(said) == 2 and said[-1].startswith("[2/2] map.jpg #")

    # Which model made which is on disk, not only in this run's output.
    for row in _usage(project):
        assert row["mode"] == "translate" and row["concurrency"] == 2
        assert row["model"] == {1: "gpt-6-luna", 2: "gpt-6-sol"}[row["candidate"]]
    shown = {c["candidate"]: c["model"] for c in _board_row(project, "map.jpg")["candidates"]}
    assert shown == {1: "gpt-6-luna", 2: "gpt-6-sol"}
    ip_apply.apply(project, [{"image": "map.jpg", "candidate": 2}])
    assert ledger.read_rows(project)[-1]["model"] == "gpt-6-sol"


def test_a_jobs_model_beats_the_flag_and_the_flag_beats_the_books_pin(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "model": "job-model"}, {**RESTORE_JOB, "candidates": 2})
    assert _planned_models(project) == {
        "map.jpg": ["job-model"],
        "compass.png": [ip_jobs.DEFAULT_MODEL_LABEL] * 2,
    }
    (project / ".harness" / "config.json").write_text(
        json.dumps({ip_jobs.MODEL_CONFIG_KEY: "pin-model"}), encoding="utf-8"
    )
    assert _planned_models(project)["compass.png"] == ["pin-model", "pin-model"]
    assert _planned_models(project, model="flag-model") == {
        "map.jpg": ["job-model"],
        "compass.png": ["flag-model", "flag-model"],
    }
    # Several on the flag: one each, in order, for a job's candidates.
    assert _planned_models(project, model="a, b")["compass.png"] == ["a", "b"]


def test_a_pinned_model_that_is_not_a_plain_id_stops_the_run(project: Path):
    _prepared(project)
    (project / ".harness" / "config.json").write_text(
        json.dumps({ip_jobs.MODEL_CONFIG_KEY: "gpt 6 luna"}), encoding="utf-8"
    )
    codex = FakeCodex()
    out = ip_jobs.generate(project, runner=codex)
    assert out["status"] == "error" and ip_jobs.MODEL_CONFIG_KEY in out["error"]
    assert codex.calls == []
    # Named on the flag, a model is what the run uses: the pin is not consulted.
    assert ip_jobs.generate(project, model="gpt-6-luna", runner=codex)["counts"]["wrote"] == 1


def test_one_candidates_crash_is_one_failed_row(project: Path):
    _prepared(project, TRANSLATE_JOB, RESTORE_JOB)
    (ip_jobs.jobs_dir(project) / "compass.png" / "prompt.txt").unlink()
    out = ip_jobs.generate(project, runner=FakeCodex())
    assert out["status"] == "partial"
    assert [row["image"] for row in out["wrote"]] == ["map.jpg"]
    assert out["failed"][0]["image"] == "compass.png"
    assert "FileNotFoundError" in out["failed"][0]["error"]
    # Both are in the usage log: the crash did not take the batch's record with it.
    assert len(ip_jobs._usage_rows(project)) == 2


@pytest.mark.parametrize("model", [7, ["ok", 7], "two words", "a&b", ["--flag"]])
def test_a_model_that_is_not_a_plain_id_is_refused(project: Path, model):
    out = ip_jobs.prepare(project, [{**TRANSLATE_JOB, "model": model}])
    assert out["status"] == "error" and "model" in json.dumps(out["invalid"])
    if isinstance(model, str):
        _prepared(project)
        codex = FakeCodex()
        out = ip_jobs.generate(project, model=model, runner=codex)
        assert out["status"] == "error" and codex.calls == []


def test_naming_another_model_keeps_the_candidates_already_made(project: Path):
    _prepared(project)
    codex = FakeCodex()
    ip_jobs.generate(project, runner=codex)
    out = ip_jobs.prepare(project, [{**TRANSLATE_JOB, "candidates": 2, "model": ["a", "b"]}])
    assert out["prepared"][0]["archived"] == 0 and out["prepared"][0]["have"] == 1
    assert ip_jobs.generate(project, runner=codex)["counts"]["wrote"] == 1
    # Candidate 2 takes the second model whether or not candidate 1 ran on the first.
    assert [_model_of(call) for call in codex.calls] == [None, "b"]


def test_a_rejected_model_stops_only_its_own_candidates(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 4, "model": ["gpt-old", "gpt-6-luna"]})
    codex = FakeCodex(
        rc=lambda cmd: 1 if "gpt-old" in cmd else 0,
        stdout=_MODEL_REJECTED,
    )
    out = ip_jobs.generate(project, concurrency=1, runner=codex)
    assert out["status"] == "partial"
    assert [_model_of(call) for call in codex.calls] == ["gpt-old", "gpt-6-luna", "gpt-6-luna"]
    assert [row["candidate"] for row in out["wrote"]] == [2, 4]
    assert out["counts"] == {"wrote": 2, "failed": 1, "not_run": 1, "todo": 4}
    assert out["error"].count("is not supported") == 1 and "--model" in out["error"]


def test_a_rejected_model_is_reported_once_however_many_runs_were_on_it(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 4, "model": "gpt-old"})
    out = ip_jobs.generate(project, runner=FakeCodex(rc=1, stdout=_MODEL_REJECTED))
    assert out["status"] == "error" and out["error"].count("is not supported") == 1


def test_an_estimate_in_parallel_is_never_less_than_one_image(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 2})
    plan = ip_jobs.generate(project, estimate=True, runner=FakeCodex())["plan"]
    assert plan["concurrency"] == ip_jobs.DEFAULT_CONCURRENCY and plan["workers"] == 2
    # Two candidates across ten workers take one image's minutes, not a fifth.
    assert plan["estimated_minutes"] == ip_jobs.ASSUMED_MINUTES_PER_IMAGE
    assert plan["sequential_minutes"] == 2 * ip_jobs.ASSUMED_MINUTES_PER_IMAGE


def test_an_estimate_in_parallel_waits_for_the_slow_image(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 2}, {**RESTORE_JOB, "candidates": 2})
    walls = [60.0] * 8 + [180.0, 180.0]
    ip_jobs.usage_path(project).write_text(
        "".join(json.dumps({"ok": True, "wall_s": wall}) + chr(10) for wall in walls),
        encoding="utf-8",
    )
    plan = ip_jobs.generate(project, estimate=True, runner=FakeCodex())["plan"]
    assert plan["minutes_measured"] is True
    assert plan["minutes_per_image"] == 1.0 and plan["minutes_slowest"] == 3.0
    assert plan["workers"] == 4 and plan["sequential_minutes"] == 4.0
    # Four at once are done when the slowest is, not after a median's worth.
    assert plan["estimated_minutes"] == 3.0
    one_at_a_time = ip_jobs.generate(project, estimate=True, concurrency=1, runner=FakeCodex())
    assert one_at_a_time["plan"]["estimated_minutes"] == 4.0


def test_a_limit_runs_a_first_wave_and_the_next_run_takes_the_rest(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 3})
    codex = FakeCodex()
    out = ip_jobs.generate(project, limit=1, runner=codex)
    assert out["counts"] == {"wrote": 1, "failed": 0, "not_run": 0, "todo": 1}
    assert out["plan"]["candidates"] == 1 and out["plan"]["held_back"] == 2
    out = ip_jobs.generate(project, runner=codex)
    assert out["counts"]["wrote"] == 2 and out["plan"]["already_have"] == 1
    assert len(codex.calls) == 3
    assert ip_jobs.generate(project, limit=0, runner=codex)["status"] == "error"


def test_a_second_generate_on_the_same_book_runs_nothing(project: Path):
    _prepared(project)
    codex = FakeCodex()
    with locks.image_lock(project):
        out = ip_jobs.generate(project, runner=codex)
    assert out["status"] == "error" and "already running" in out["error"]
    assert codex.calls == [] and out["wrote"] == []
    assert not ip_jobs.usage_path(project).exists()
    # The lock goes with the run that held it.
    assert ip_jobs.generate(project, runner=codex)["counts"]["wrote"] == 1
    assert not locks.image_lock_path(project).exists()


def test_an_estimate_does_not_wait_on_a_running_generate(project: Path):
    _prepared(project)
    with locks.image_lock(project):
        out = ip_jobs.generate(project, estimate=True, runner=FakeCodex())
    assert out["status"] == "ok" and out["estimate"] is True


def test_prepare_is_refused_while_a_run_holds_the_book(project: Path):
    _prepared(project)
    assert ip_jobs.generate(project, runner=FakeCodex())["counts"]["wrote"] == 1
    job_dir = ip_jobs.jobs_dir(project) / "map.jpg"
    before = (job_dir / "prompt.txt").read_text("utf-8")
    bolder = {**TRANSLATE_JOB, "instruction": "Bolder."}
    with locks.image_lock(project):
        out = ip_jobs.prepare(project, [bolder])
    assert out["status"] == "error" and "holds this book" in out["error"]
    # Nothing moved under the run: not its prompt, not the candidate it has made.
    assert (job_dir / "prompt.txt").read_text("utf-8") == before
    assert ip_jobs.existing_candidates(job_dir) == [1]
    assert ip_jobs.prepare(project, [bolder])["counts"]["archived_candidates"] == 1


def test_a_candidate_made_while_this_run_waited_is_not_made_again(project: Path):
    _prepared(project)
    candidate = ip_jobs.candidate_path(ip_jobs.jobs_dir(project) / "map.jpg", 1)

    # Another run ends between this one's count and its lock; the login probe
    # is what happens in between.
    def probe(argv, *, env, cwd, timeout):
        _image(candidate, fmt="PNG")
        return 0, "", "Logged in using ChatGPT"

    codex = FakeCodex()
    out = ip_jobs.generate(project, runner=codex, cli_bin="python", prober=probe)
    assert out["status"] == "ok" and codex.calls == []
    assert out["counts"] == {"wrote": 0, "failed": 0, "not_run": 0, "todo": 0}
    assert "nothing was spent" in out["instructions"]


def test_a_run_that_starts_nothing_still_reports_every_count(project: Path):
    _prepared(project)
    with locks.image_lock(project):
        busy = ip_jobs.generate(project, runner=FakeCodex())
    assert busy["counts"] == {"wrote": 0, "failed": 0, "not_run": 1, "todo": 1}
    ip_jobs.generate(project, runner=FakeCodex())
    done = ip_jobs.generate(project, runner=FakeCodex())
    assert done["counts"] == {"wrote": 0, "failed": 0, "not_run": 0, "todo": 0}


def test_another_sessions_picture_is_never_taken_for_this_jobs(project: Path):
    def someone_else(cmd, *, input_text, cwd):
        # Another Codex on the machine made a picture while this job ran, and
        # this job made none.
        _image(
            Path(os.environ["CODEX_HOME"]) / "generated_images" / "their-thread" / "exec-1.png",
            fmt="PNG",
        )
        return 0, json.dumps({"type": "thread.started", "thread_id": "this-thread"}), ""

    _prepared(project)
    out = ip_jobs.generate(project, concurrency=1, runner=someone_else)
    assert out["counts"]["failed"] == 1 and "left no image" in out["failed"][0]["error"]
    assert not (ip_jobs.jobs_dir(project) / "map.jpg" / "cand_01.png").exists()


def test_cli_generate_passes_the_wave_and_the_models_through(project: Path, capsys, monkeypatch):
    seen: dict = {}

    def fake_generate(project_dir, **kwargs):
        seen.update(kwargs)
        kwargs["progress"]("[1/1] map.jpg #1 ok 70s (a)")
        return {"status": "ok", "wrote": [], "failed": []}

    monkeypatch.setattr(ip_jobs, "generate", fake_generate)
    assert cli.main(["generate", "--project", str(project)]) == 0
    assert seen["concurrency"] == ip_jobs.DEFAULT_CONCURRENCY and seen["limit"] is None
    assert cli.main(
        ["generate", "--project", str(project), "--limit", "10", "--model", "a,b"]
    ) == 0
    assert seen["limit"] == 10 and seen["model"] == "a,b"
    captured = capsys.readouterr()
    # Progress is for whoever is watching; stdout stays the one JSON object.
    assert "[1/1] map.jpg #1 ok 70s (a)" in captured.err
    assert "[1/1]" not in captured.out


# --- the board: triage, checks, and what the user said ----------------------

TRIAGE_ROWS = [
    {"image": "map.jpg", "verdict": "translate", "finding": "Two labels, both legible.",
     "labels": {"NORTH": "NORTE", "SCHOOL-ROOM": "SALÓN DE CLASE"}},
    {"image": "images/compass.png", "verdict": "leave", "finding": "No lettering."},
]


def test_the_board_follows_an_image_through_every_stage(project: Path):
    stages = lambda: {row["image"]: row["stage"] for row in ip_board.build(project)["images"]}
    assert stages() == {
        "map.jpg": "untriaged", "compass.png": "untriaged",
        "gone.jpg": "missing", "cover.jpg": "untriaged",
    }

    out = ip_board.save_triage(project, TRIAGE_ROWS)
    assert out["counts"] == {
        "requested": 2, "recorded": 2, "invalid": 0, "triaged": 2, "untriaged": 2,
        "by_verdict": {"translate": 1, "leave": 1},
    }
    assert (stages()["map.jpg"], stages()["compass.png"]) == ("proposed", "leave")
    row = _board_row(project, "map.jpg")
    assert row["labels"]["SCHOOL-ROOM"] == "SALÓN DE CLASE" and row["lettering"]

    _prepared(project)
    assert stages()["map.jpg"] == "queued"

    ip_jobs.generate(project, runner=FakeCodex(size=(1024, 1024)))
    row = _board_row(project, "map.jpg")
    assert row["stage"] == "review"
    # 1:1 against a 3:2 original is flagged for the human, not hidden.
    assert "aspect" in row["candidates"][0]["flags"][0] and row["flagged"] == ["candidate_flag"]
    assert [p["kind"] for p in row["pictures"]] == ["original"]

    ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    row = _board_row(project, "map.jpg")
    assert row["stage"] == "replaced"
    # Once replaced, the publisher's file is the backup and the page shows both.
    assert [(p["kind"], p["source"]) for p in row["pictures"]] == [
        ("original", "images_original"), ("current", "images"),
    ]


def test_a_candidate_made_after_the_decision_is_up_for_review_again(project: Path):
    _with_candidate(project)
    ip_apply.apply(project, [{"image": "map.jpg", "verdict": "redo", "note": "accent missing"}])
    # Sent back: the agent owes a new job, and there is nothing new to look at.
    assert _board_row(project, "map.jpg")["stage"] == "proposed"

    later = time.time() + 60
    os.utime(project / ".harness" / "images" / "jobs" / "map.jpg" / "cand_01.png", (later, later))
    assert _board_row(project, "map.jpg")["stage"] == "review"


def test_triage_is_all_or_nothing_and_merges_by_image(project: Path):
    bad = ip_board.save_triage(project, TRIAGE_ROWS + [
        {"image": "stray.jpg", "verdict": "leave", "finding": "Not in the book."},
        {"image": "map.jpg", "verdict": "fix", "finding": ""},
    ])
    assert bad["status"] == "error" and not ip_board.triage_path(project).exists()
    problems = {row["index"]: " ".join(row["problems"]) for row in bad["invalid"]}
    assert "does not reference" in problems[2]
    assert "verdict" in problems[3] and "finding is required" in problems[3]

    ip_board.save_triage(project, TRIAGE_ROWS)
    ip_board.save_triage(project, [
        {"image": "compass.png", "verdict": "restore", "finding": "Foxed lower left."},
    ])
    saved = ip_board.load_triage(project)
    assert saved["compass.png"]["verdict"] == "restore" and saved["map.jpg"]["verdict"] == "translate"

    ip_board.save_triage(project, [TRIAGE_ROWS[1]], replace=True)
    assert set(ip_board.load_triage(project)) == {"compass.png"}


def test_a_check_is_about_one_file_and_dies_with_it(project: Path):
    _with_candidate(project)
    refused = ip_board.save_checks(project, [
        {"image": "map.jpg", "candidate": 1, "ok": False},
        {"image": "map.jpg", "candidate": 2, "ok": True},
    ])
    assert refused["status"] == "error" and not ip_board.checks_path(project).exists()

    out = ip_board.save_checks(project, [
        {"image": "map.jpg", "candidate": 1, "ok": False, "finding": "NORTE lost its E."},
    ])
    assert out["counts"]["not_ok"] == 1
    row = _board_row(project, "map.jpg")
    assert row["candidates"][0]["check"] == {"ok": False, "finding": "NORTE lost its E."}
    assert "check_failed" in row["flagged"]
    assert ip_board.summary(project)["counts"]["unchecked_candidates"] == 0

    # The same number, generated again, is a picture nobody has looked at.
    _image(project / ".harness" / "images" / "jobs" / "map.jpg" / "cand_01.png",
           (1536, 1024), (1, 2, 3), fmt="PNG")
    assert _board_row(project, "map.jpg")["candidates"][0]["check"] is None
    assert ip_board.summary(project)["counts"]["unchecked_candidates"] == 1


def test_a_request_is_outstanding_until_the_job_says_the_same(project: Path):
    drift = lambda image: _board_row(project, image)["drift"]
    ip_board.save_triage(project, TRIAGE_ROWS)

    # Triage said leave it; the user says otherwise. That is a job to write.
    saved = ip_feedback.save_image(project, "compass.png", request={
        "verdict": "restore", "note": "Clean the foxing, keep the plate mark.",
    })
    assert saved["status"] == "ok" and drift("compass.png") == ["needs_job"]
    assert _board_row(project, "compass.png")["stage"] == "proposed"
    # And the other way: what they set to leave alone is no longer a job to write.
    ip_feedback.save_image(project, "map.jpg", request={"verdict": "leave"})
    assert ip_board.summary(project)["proposed"] == [
        {"image": "compass.png", "verdict": "restore", "by": "user"},
    ]
    ip_feedback.save_image(project, "map.jpg", request=None)
    assert {"image": "map.jpg", "verdict": "translate", "by": "triage"} in (
        ip_board.summary(project)["proposed"]
    )

    # A note alone, on an image nobody means to touch, asks for a second look.
    ip_feedback.save_image(project, "compass.png", request={"note": "Is that a signature?"},
                           now=_stamp(60))
    assert drift("compass.png") == ["note_unaddressed"]
    ip_feedback.save_image(project, "compass.png", request=None)
    assert drift("compass.png") == [] and "compass.png" not in ip_feedback.load(project)

    _prepared(project)
    assert drift("map.jpg") == []
    ip_feedback.save_image(project, "map.jpg", request={
        "verdict": "restore",
        "labels": {"NORTH": "NORTE"},
        "candidates": 2,
        "note": "Tighter letter-spacing.",
    }, now=_stamp(60))
    assert drift("map.jpg") == [
        "mode_differs", "labels_differ", "candidates_differ", "note_newer_than_job",
    ]
    listed = ip_board.summary(project)["requests"]
    assert [r["image"] for r in listed] == ["map.jpg"] and listed[0]["labels"] == {"NORTH": "NORTE"}

    # Changing only the count must not make an old note look new again.
    before = ip_feedback.load(project)["map.jpg"]["request"]["note_updated"]
    ip_feedback.save_image(project, "map.jpg", request={
        "note": "Tighter letter-spacing.", "candidates": 3,
    }, now=_stamp(120))
    assert ip_feedback.load(project)["map.jpg"]["request"]["note_updated"] == before

    # Once the job carries what was asked, nothing is left to do.
    ip_jobs.prepare(project, [{**TRANSLATE_JOB, "candidates": 3,
                               "instruction": "Tighter letter-spacing."}])
    job = ip_jobs.load_manifest(project)["jobs"][0]
    job["prepared_at"] = _stamp(180)
    ip_jobs.manifest_path(project).write_text(
        json.dumps({"version": 1, "jobs": [job]}, ensure_ascii=False), "utf-8"
    )
    assert drift("map.jpg") == []

    ip_feedback.save_image(project, "map.jpg", request={"verdict": "leave"})
    assert drift("map.jpg") == ["job_unwanted"]


@pytest.mark.parametrize(
    "request_,needle",
    [
        ({"verdict": "polish"}, "verdict"),
        ({"labels": {"NORTH": ""}}, "labels must map"),
        ({"candidates": 9}, "candidates"),
        ({"note": 7}, "note must be text"),
    ],
)
def test_a_bad_request_is_refused_and_writes_nothing(project: Path, request_, needle):
    out = ip_feedback.save_image(project, "map.jpg", request=request_)
    assert out["status"] == "error" and needle in " ".join(out["problems"])
    assert not ip_feedback.feedback_path(project).exists()


def test_a_pick_needs_a_candidate_to_pick(project: Path):
    out = ip_feedback.save_image(project, "map.jpg", pick={"verdict": "accept", "candidate": 1})
    assert "no prepared job" in out["problems"][0]
    _with_candidate(project)
    out = ip_feedback.save_image(project, "map.jpg", pick={"verdict": "accept", "candidate": 4})
    assert "candidate 4 does not exist" in out["problems"][0]


def test_picks_become_decisions_and_stop_being_outstanding_once_applied(project: Path):
    _with_candidate(project)
    ip_feedback.save_image(project, "map.jpg", request={"note": "Keep the border."})
    ip_feedback.save_image(project, "map.jpg", pick={
        "verdict": "accept", "candidate": 1, "note": "Lettering is right.",
    })
    # Saving the pick left the request from the other control where it was.
    assert ip_feedback.load(project)["map.jpg"]["request"]["note"] == "Keep the border."

    decisions, stale = ip_board.decisions_from_picks(ip_board.build(project))
    assert stale == [] and decisions == [
        {"image": "map.jpg", "note": "Lettering is right.", "candidate": 1},
    ]

    # A dry run writes no ledger row, so the pick is still there to apply.
    assert ip_apply.apply(project, decisions, dry_run=True)["counts"]["planned"] == 1
    assert _board_row(project, "map.jpg")["pick"]["pending"] is True

    assert ip_apply.apply(project, decisions)["counts"]["applied"] == 1
    assert _board_row(project, "map.jpg")["pick"]["pending"] is False
    assert ip_board.decisions_from_picks(ip_board.build(project)) == ([], [])


def test_a_pick_made_on_a_regenerated_candidate_is_never_applied(project: Path, capsys):
    _with_candidate(project)
    ip_feedback.save_image(project, "map.jpg", pick={"verdict": "accept", "candidate": 1})
    before = (project / "images" / "map.jpg").read_bytes()
    _image(project / ".harness" / "images" / "jobs" / "map.jpg" / "cand_01.png",
           (1536, 1024), (1, 2, 3), fmt="PNG")

    row = _board_row(project, "map.jpg")
    assert row["pick"]["stale"] is True and "stale_pick" in row["flagged"]
    code, out, _ = _run(capsys, ["apply", "--project", str(project), "--from-board"])
    assert code == 1 and out["status"] == "error" and out["counts"]["applied"] == 0
    assert "pick again" in out["refused"][0]["problems"][0]
    assert (project / "images" / "map.jpg").read_bytes() == before


def test_cli_walks_from_triage_to_an_applied_pick(project: Path, capsys, tmp_path: Path):
    rows = tmp_path / "triage_rows.json"
    rows.write_text(json.dumps(TRIAGE_ROWS, ensure_ascii=False), "utf-8")
    code, out, _ = _run(capsys, ["triage", "--project", str(project), "--json-file", str(rows)])
    assert code == 0 and out["counts"]["recorded"] == 2

    _with_candidate(project)
    checks = tmp_path / "check_rows.json"
    checks.write_text(json.dumps({"checks": [
        {"image": "map.jpg", "candidate": 1, "ok": True, "finding": "Both labels right."},
    ]}), "utf-8")
    code, out, _ = _run(capsys, ["check", "--project", str(project), "--json-file", str(checks)])
    assert code == 0 and out["counts"]["recorded"] == 1

    with pytest.raises(SystemExit) as exc:
        cli.main(["apply", "--project", str(project), "--from-board"])
    assert "no picks are waiting" in json.loads(str(exc.value))["error"]

    ip_feedback.save_image(project, "map.jpg", pick={"verdict": "accept", "candidate": 1})
    ip_feedback.save_image(project, "compass.png", request={"verdict": "restore"})
    # Nothing listens on this port: the board still reports, and says so.
    code, out, _ = _run(capsys, ["board", "--project", str(project),
                                 "--base-url", "http://127.0.0.1:9"])
    assert code == 0 and out["server_running"] is False
    assert out["url"] == "http://127.0.0.1:9/image-pass/book"
    assert "not answering" in out["instructions"]
    assert out["picks"] == [{"image": "map.jpg", "note": None, "candidate": 1}]
    assert [(r["image"], r["reasons"]) for r in out["requests"]] == [("compass.png", ["needs_job"])]
    assert out["jobs"][0]["candidates"][0]["checked"] is True
    assert out["untriaged"] == ["cover.jpg"]

    code, out, _ = _run(capsys, ["apply", "--project", str(project), "--from-board", "--dry-run"])
    assert code == 0 and out["counts"]["planned"] == 1
    code, out, _ = _run(capsys, ["apply", "--project", str(project), "--from-board"])
    assert code == 0 and out["applied"][0]["image"] == "map.jpg"


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


def test_a_candidate_retried_after_an_apply_is_still_drawn_from_the_original(project: Path):
    _prepared(project, {**TRANSLATE_JOB, "candidates": 2})
    original = (project / "images" / "map.jpg").read_bytes()
    # Candidate 2 fails the first time round, and candidate 1 is applied.
    flaky = FakeCodex(rc=lambda cmd: 1 if "run_02" in " ".join(cmd) else 0)
    out = ip_jobs.generate(project, concurrency=1, runner=flaky)
    assert out["counts"] == {"wrote": 1, "failed": 1, "not_run": 0, "todo": 2}
    ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    assert (project / "images" / "map.jpg").read_bytes() != original
    # No re-prepare in between: the job still names images/map.jpg as its input.
    codex = FakeCodex()
    assert ip_jobs.generate(project, runner=codex)["counts"]["wrote"] == 1
    assert Path(codex.calls[0]["cmd"][3]).read_bytes() == original


def test_a_save_that_fails_leaves_no_temp_file_in_the_book(project: Path, monkeypatch):
    _with_candidate(project)
    before = (project / "images" / "map.jpg").read_bytes()

    def full_disk(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(ip_apply.os, "replace", full_disk)
    out = ip_apply.apply(project, [{"image": "map.jpg", "candidate": 1}])
    assert out["status"] == "error" and "could not write" in out["refused"][0]["problems"][0]
    assert (project / "images" / "map.jpg").read_bytes() == before
    # A leftover would sit beside the book's images and ship in the EPUB.
    assert [p.name for p in project.rglob("*") if p.name.endswith(".image-pass.tmp")] == []


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


COVER_JOB = {"image": "cover.jpg", "mode": "cover", "instruction": "A globe.", "candidates": 2}


def test_a_cover_made_from_nothing_is_never_backed_up_as_the_original(project: Path):
    (project / "source.txt").write_text(SOURCE.replace("[IMAGE:images/gone.jpg:NOT ON DISK]", ""), "utf-8")
    ip_jobs.prepare(project, [COVER_JOB])
    ip_jobs.generate(project, runner=FakeCodex(size=(1024, 1536)))
    ip_apply.apply(project, [{"image": "cover.jpg", "candidate": 1}])

    out = ip_apply.apply(project, [{"image": "cover.jpg", "candidate": 2}])
    assert out["applied"][0]["backup"] is None
    assert out["applied"][0]["backup_action"].startswith("none (image-pass made this file")
    assert not (project / "images_original" / "cover.jpg").exists()
    assert ledger.read_rows(project)[-1]["created"] is True

    # So the original is still its absence, and revert still gets back to it.
    assert ip_apply.revert(project, ["cover.jpg"])["reverted"][0]["action"].startswith("removed")
    assert not (project / "images" / "cover.jpg").exists()
    assert ip_apply.verify(project)["status"] == "ok"


def test_a_redo_of_a_cover_made_from_nothing_starts_from_nothing_again(project: Path):
    ip_jobs.prepare(project, [COVER_JOB])
    ip_jobs.generate(project, runner=FakeCodex(size=(1024, 1536)))
    ip_apply.apply(project, [{"image": "cover.jpg", "candidate": 1}])

    out = ip_jobs.prepare(project, [{**COVER_JOB, "instruction": "A globe on a desk."}])
    assert out["prepared"][0]["input_from"] is None
    codex = FakeCodex(size=(1024, 1536))
    ip_jobs.generate(project, runner=codex)
    assert all("-i" not in call["cmd"] for call in codex.calls)
    # Asked for by name, the cover that is there is what the redo is drawn from.
    out = ip_jobs.prepare(project, [{**COVER_JOB, "input": "current"}])
    assert out["prepared"][0]["input_from"] == "original"


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


# --- composite: one candidate's patch over another picture's pixels -----------

def _pixel(path: Path, at) -> tuple:
    with Image.open(path) as image:
        return image.convert("RGB").getpixel(at)


def _job_dir(project: Path) -> Path:
    return ip_jobs.jobs_dir(project) / "map.jpg"


def test_a_composite_keeps_the_base_outside_the_outline(project: Path):
    _with_candidate(project)  # a green 1536 x 1024 candidate for the red 300 x 200 map
    original = project / "images" / "map.jpg"
    red = _pixel(original, (10, 10))

    out = ip_composite.composite(
        project, [{"image": "images/map.jpg", "from": 1, "regions": [[100, 50, 200, 150]], "feather": 0}]
    )
    assert out["status"] == "ok" and out["counts"]["made"] == 1, out
    made = out["made"][0]
    # Numbered after every slot `generate` can fill, at the original's own size.
    assert made["candidate"] == ip_jobs.MAX_CANDIDATES + 1 and made["size"] == [300, 200]
    assert made["base"] == {"kind": "original"} and made["from"] == {"kind": "candidate", "candidate": 1}
    assert 0.15 < made["changed_share"] < 0.18  # 100 x 100 of 300 x 200

    path = Path(made["path"])
    assert _pixel(path, (10, 10)) == red and _pixel(path, (99, 49)) == red
    assert _pixel(path, (150, 100)) == (10, 120, 60)
    assert not (project / "images_original").exists()

    # It is a candidate like any other: on the board with its outline, and applied.
    shown = _board_row(project, "map.jpg")["candidates"]
    assert [c["candidate"] for c in shown] == [1, made["candidate"]]
    assert shown[0]["composite"] is None
    assert shown[1]["composite"]["regions"] == [[[100, 50], [200, 50], [200, 150], [100, 150]]]
    applied = ip_apply.apply(project, [{"image": "map.jpg", "candidate": made["candidate"]}])
    assert applied["counts"]["applied"] == 1
    assert ledger.read_rows(project)[-1]["composite"]["base"]["kind"] == "original"
    assert _size(project / "images" / "map.jpg") == (300, 200)


def test_a_composite_lines_up_a_candidate_that_came_back_padded(project: Path):
    """The image tool cannot make a very wide strip: it hands back the drawing
    with white bands above and below. The patch still has to land where it
    belongs in the original."""
    from PIL import ImageDraw

    strip = Image.new("RGB", (400, 100), (255, 255, 255))
    ImageDraw.Draw(strip).rectangle([200, 40, 239, 59], fill=(0, 0, 0))
    strip.save(project / "images" / "compass.png")
    _prepared(project, {"image": "compass.png", "mode": "restore", "instruction": "x"})
    padded = Image.new("RGB", (1200, 600), (255, 255, 255))  # 3x, centred: rows 150-449
    ImageDraw.Draw(padded).rectangle([600, 270, 719, 329], fill=(0, 0, 255))
    padded.save(ip_jobs.jobs_dir(project) / "compass.png" / "cand_01.png")

    out = ip_composite.composite(
        project, [{"image": "compass.png", "from": 1, "regions": [[190, 30, 250, 70]], "feather": 0}]
    )
    made = out["made"][0]
    assert made["padded_source"] is True and made["size"] == [400, 100]
    path = Path(made["path"])
    assert _pixel(path, (220, 50)) == (0, 0, 255)       # the square, where the square was
    assert _pixel(path, (195, 50)) == (255, 255, 255)   # and not a pixel to its left
    assert _pixel(path, (245, 50)) == (255, 255, 255)


def test_a_composite_slides_the_patch_to_where_its_surroundings_match(project: Path, tmp_path: Path):
    """Two renderings of one drawing sit a few pixels apart. The patch is put
    where the drawing around it lines up, not where the arithmetic says."""
    from PIL import ImageChops

    drawing = Image.open(_drawing(tmp_path / "drawing.png", (300, 200), seed=7)).convert("RGB")
    drawing.save(project / "images" / "compass.png")
    _prepared(project, {"image": "compass.png", "mode": "restore", "instruction": "x"})
    ImageChops.offset(drawing, 2, -1).save(ip_jobs.jobs_dir(project) / "compass.png" / "cand_01.png")

    out = ip_composite.composite(
        project, [{"image": "compass.png", "from": 1, "regions": [[120, 80, 180, 120]]}]
    )
    region = out["made"][0]["regions"][0]
    assert region["offset"] == [2, -1]
    assert region["surround_difference"] < region["surround_difference_unmoved"]
    # Shifted back, the candidate is the base: nothing is left to differ.
    assert out["made"][0]["changed_share"] == 0
    assert [w["code"] for w in out["warnings"]] == ["nothing_changed"]


def test_a_composite_can_be_built_on_an_archived_candidate(project: Path):
    """A redo that fixed one label and broke another: keep the earlier
    candidate, take only the fixed label from the new one."""
    _with_candidate(project, color=(10, 120, 60))
    ip_jobs.prepare(project, [{**TRANSLATE_JOB, "instruction": "Fix the one label."}])
    ip_jobs.generate(project, runner=FakeCodex(color=(0, 0, 200)))
    earlier = next((_job_dir(project) / "previous").glob("*/cand_01.png"))
    base = earlier.relative_to(_job_dir(project)).as_posix()

    out = ip_composite.composite(
        project, [{"image": "map.jpg", "from": 1, "base": base, "regions": [[0, 0, 100, 100]], "feather": 0}]
    )
    made = out["made"][0]
    assert made["base"] == {"kind": "previous", "path": base} and made["size"] == [1536, 1024]
    assert _pixel(Path(made["path"]), (50, 50)) == (0, 0, 200)
    assert _pixel(Path(made["path"]), (800, 600)) == (10, 120, 60)


@pytest.mark.parametrize(
    "change,needle",
    [
        ({"from": 9}, "candidate 9 does not exist"),
        ({"from": "original"}, "not a candidate number"),
        ({"base": "../../../source.txt"}, "previous/ folder"),
        ({"base": 1}, "same file"),
        ({"regions": []}, "non-empty list"),
        ({"regions": [[10, 10, 5, 50]]}, "x1 > x0"),
        ({"regions": [[0, 0, 400, 100]]}, "outside the base picture"),
        ({"scale": 9}, "scale must be"),
        ({"feather": -1}, "feather must be"),
        ({"image": "compass.png"}, "no prepared job"),
        ({"outline": True}, "unknown field"),
    ],
)
def test_composite_is_all_or_nothing(project: Path, change, needle):
    _with_candidate(project)
    good = {"image": "map.jpg", "from": 1, "regions": [[100, 50, 200, 150]]}
    out = ip_composite.composite(project, [good, {**good, **change}])
    assert out["status"] == "error" and out["counts"]["made"] == 0
    assert needle in " ".join(out["invalid"][0]["problems"]), out["invalid"]
    assert ip_jobs.existing_candidates(_job_dir(project)) == [1]


def test_a_composite_dry_run_measures_and_writes_nothing(project: Path):
    _with_candidate(project)
    out = ip_composite.composite(
        project, [{"image": "map.jpg", "from": 1, "regions": [[100, 50, 200, 150]]}], dry_run=True
    )
    assert out["counts"] == {"requested": 1, "made": 0, "planned": 1, "invalid": 0, "warnings": 0}
    assert out["planned"][0]["candidate"] == ip_jobs.MAX_CANDIDATES + 1
    assert ip_jobs.existing_candidates(_job_dir(project)) == [1]


def test_a_composite_never_stands_in_for_a_candidate_generate_owes(project: Path):
    _with_candidate(project)
    ip_composite.composite(project, [{"image": "map.jpg", "from": 1, "regions": [[0, 0, 50, 50]]}])
    # Asking for a second candidate still runs Codex once: the composite is not it.
    ip_jobs.prepare(project, [{**TRANSLATE_JOB, "candidates": 2}])
    codex = FakeCodex()
    assert ip_jobs.generate(project, runner=codex)["counts"]["wrote"] == 1
    assert len(codex.calls) == 1
    assert ip_jobs.existing_candidates(_job_dir(project)) == [1, 2, ip_jobs.MAX_CANDIDATES + 1]


def test_the_board_still_counts_a_candidate_owed_beside_a_composite(project: Path):
    _with_candidate(project)
    ip_composite.composite(project, [{"image": "map.jpg", "from": 1, "regions": [[0, 0, 50, 50]]}])
    ip_jobs.prepare(project, [{**TRANSLATE_JOB, "candidates": 2}])
    # Two pictures on the card, and the job's second candidate is not one of them.
    assert ip_board.summary(project)["jobs"][0]["missing"] == 1
    # Once both have been looked at and passed over, the card is still waiting
    # on `generate`, not left alone.
    ip_apply.apply(project, [{"image": "map.jpg", "verdict": "skip", "note": "neither"}])
    assert _board_row(project, "map.jpg")["stage"] == ip_board.STAGE_QUEUED


def test_a_composite_can_be_made_again_in_place_but_never_over_a_generated_candidate(project: Path):
    _with_candidate(project)
    row = {"image": "map.jpg", "from": 1, "regions": [[0, 0, 50, 50]], "feather": 0}
    first = ip_composite.composite(project, [row])["made"][0]
    assert _pixel(Path(first["path"]), (150, 100)) != (10, 120, 60)

    again = ip_composite.composite(
        project, [{**row, "regions": [[100, 50, 200, 150]], "candidate": first["candidate"]}]
    )["made"][0]
    assert again["candidate"] == first["candidate"]
    assert _pixel(Path(again["path"]), (150, 100)) == (10, 120, 60)
    assert ip_jobs.existing_candidates(_job_dir(project)) == [1, first["candidate"]]

    for number, needle in ((1, "composite's number"), (first["candidate"] + 1, None)):
        out = ip_composite.composite(project, [{**row, "candidate": number}], dry_run=True)
        assert (out["status"] == "error") == bool(needle)
    # A file in a composite's range that Codex-style tooling put there is not ours.
    _image(_job_dir(project) / "cand_07.png", fmt="PNG")
    out = ip_composite.composite(project, [{**row, "candidate": 7}])
    assert "not a composite" in out["invalid"][0]["problems"][0]


def test_a_composites_description_is_archived_with_it_and_dies_with_its_file(project: Path):
    _with_candidate(project)
    out = ip_composite.composite(project, [{"image": "map.jpg", "from": 1, "regions": [[0, 0, 50, 50]]}])
    path = Path(out["made"][0]["path"])
    assert ip_composite.load_sidecar(path)["regions"]

    # The description is about one file: over another picture it says nothing.
    _image(path, (300, 200), (1, 2, 3), fmt="PNG")
    assert ip_composite.load_sidecar(path) is None
    assert _board_row(project, "map.jpg")["candidates"][-1]["composite"] is None

    ip_jobs.prepare(project, [{**TRANSLATE_JOB, "instruction": "Start over."}])
    assert not list(_job_dir(project).glob("cand_*"))
    assert len(list((_job_dir(project) / "previous").glob("*/cand_05.composite.json"))) == 1


def test_cli_composite_prints_one_json_object(project: Path, capsys, tmp_path: Path):
    _with_candidate(project)
    rows = tmp_path / "composites.json"
    rows.write_text(json.dumps({"composites": [
        {"image": "map.jpg", "from": 1, "regions": [[[100, 50], [200, 50], [150, 150]]], "scale": 2},
    ]}), "utf-8")
    code, out, _ = _run(capsys, ["composite", "--project", str(project), "--json-file", str(rows)])
    assert code == 0 and out["made"][0]["size"] == [600, 400]
    # The outline is reported in the composite's own pixels, where the page draws it.
    assert out["made"][0]["regions"][0]["polygon"] == [[200, 100], [400, 100], [300, 300]]
    code, out, _ = _run(capsys, ["board", "--project", str(project)])
    assert out["jobs"][0]["candidates"][-1]["composite"]["from"] == {"kind": "candidate", "candidate": 1}
