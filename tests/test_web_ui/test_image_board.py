"""The image board (``/image-pass/<id>``).

The board's state is built and tested in ``src/image_pass`` — see
``tests/test_image_pass.py``. What these tests pin is the web UI's part, which
is deliberately thin: it puts URLs on the rows in place of filesystem paths,
serves the two kinds of picture the existing image route does not reach (the
publisher's original behind a replacement, and a generated candidate), and
takes the one thing the page writes.

That one thing matters most. ``feedback.json`` is how what the user said on the
page reaches the next step of a run, so the assertions are about it arriving on
disk as sent, a card's two halves not clobbering each other, and nothing being
written for an image the book does not have.
"""

from __future__ import annotations

import json

import pytest
from PIL import Image

from src.image_pass import apply as ip_apply
from src.image_pass import board as ip_board
from src.image_pass import composite as ip_composite
from src.image_pass import cover_drafts as ip_drafts
from src.image_pass import feedback as ip_feedback
from src.image_pass import jobs as ip_jobs
from src.image_pass import ledger
from web_ui.app import app
from web_ui.i18n import STRINGS


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _image(path, size=(300, 200), color=(200, 30, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


@pytest.fixture
def book(tmp_path, monkeypatch):
    """A book with two images, one of them with a job and a candidate."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "imgbook"
    (proj_dir / "chapters").mkdir(parents=True)
    (proj_dir / "project.json").write_text(json.dumps({"title": "Img Book"}), "utf-8")
    (proj_dir / "source.txt").write_text(
        "[IMAGE:images/map.jpg:A MAP.]\n\n[IMAGE:images/plates/dial.png]\n", "utf-8"
    )
    (proj_dir / "chapters" / "chapter_01.txt").write_text(
        "[IMAGE:images/map.jpg:UN MAPA.]\n", "utf-8"
    )
    _image(proj_dir / "images" / "map.jpg")
    _image(proj_dir / "images" / "plates" / "dial.png", (120, 120))

    out = ip_jobs.prepare(proj_dir, [{
        "image": "map.jpg", "mode": "translate", "instruction": "Keep the serif capitals.",
        "labels": {"NORTH": "NORTE"},
    }])
    assert out["status"] == "ok", out
    _image(proj_dir / ".harness" / "images" / "jobs" / "map.jpg" / "cand_01.png",
           (600, 400), (10, 120, 60))

    import web_ui.app as app_module
    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _rows(client):
    data = client.get("/api/project/imgbook/image-pass").get_json()
    return {row["image"]: row for row in data["images"]}, data


def _post(client, payload):
    return client.post("/api/project/imgbook/image-pass/feedback", json=payload)


# --- the page ---------------------------------------------------------------

def test_the_page_renders_its_shell_in_both_languages(client, book):
    page = client.get("/image-pass/imgbook")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'data-project="imgbook"' in html and "image_board.js" in html
    assert "Awaiting triage" in html  # the script's strings ride in the page

    client.set_cookie("reader_lang", "es")
    assert "Sin clasificar" in client.get("/image-pass/imgbook").get_data(as_text=True)


def test_an_unknown_or_unsafe_project_is_refused(client, book):
    assert client.get("/image-pass/nosuchbook").status_code == 404
    assert client.get("/image-pass/..").status_code in (400, 404)
    assert client.get("/api/project/nosuchbook/image-pass").status_code == 404
    assert _post(client, {"image": "map.jpg", "request": {"note": "x"}}).status_code == 200
    assert client.post("/api/project/nosuchbook/image-pass/feedback",
                       json={"image": "map.jpg"}).status_code == 404


def test_every_string_has_its_spanish_twin():
    """`get_strings` falls back a whole table at a time, so a key missing from
    `es` renders as nothing at all on the Spanish page rather than in English."""
    en, es = STRINGS["en"], STRINGS["es"]
    top = {key for key in en if key.startswith("imgboard_")}
    assert top and top == {key for key in es if key.startswith("imgboard_")}

    def shape(table):
        return {
            key: sorted(value) if isinstance(value, dict) else None
            for key, value in table.items()
        }

    assert shape(en["imgboard_js"]) == shape(es["imgboard_js"])
    # The script looks these up by the server's own codes.
    assert set(en["imgboard_js"]["stages"]) == set(ip_board.STAGES)
    assert set(en["imgboard_js"]["verdicts"]) == set(ip_feedback.VERDICTS)
    assert set(en["imgboard_js"]["tracks"]) == set(ip_board.TRACKS)


# --- the state --------------------------------------------------------------

def test_rows_carry_urls_and_never_a_path_on_disk(client, book):
    rows, data = _rows(client)
    assert data["stages"] == list(ip_board.STAGES)
    assert data["counts"]["review"] == 1 and data["counts"]["untriaged"] == 2

    row = rows["map.jpg"]
    assert row["stage"] == "review" and row["chapter"] == "chapter_01"
    original, candidate = row["pictures"][0], row["candidates"][0]
    assert original["url"].startswith("/projects/imgbook/images/map.jpg?v=")
    assert candidate["url"].startswith("/projects/imgbook/image-pass/candidate/map.jpg/1?v=")
    assert str(book) not in json.dumps(data)

    # Both URLs resolve to the files they name.
    assert client.get(original["url"]).data == (book / "images" / "map.jpg").read_bytes()
    served = client.get(candidate["url"])
    assert served.status_code == 200 and served.mimetype == "image/png"

    # An image in a subfolder keeps its slash.
    assert rows["plates/dial.png"]["pictures"][0]["url"].startswith(
        "/projects/imgbook/images/plates/dial.png?v="
    )


def test_a_composite_reaches_the_page_with_its_outline_and_can_be_picked(client, book):
    made = ip_composite.composite(
        book, [{"image": "map.jpg", "from": 1, "regions": [[100, 50, 200, 150]]}]
    )["made"][0]
    rows, data = _rows(client)
    plain, composite = rows["map.jpg"]["candidates"]
    assert plain["composite"] is None
    assert composite["composite"] == {
        "from": {"kind": "candidate", "candidate": 1},
        "base": {"kind": "original"},
        "regions": [[[100, 50], [200, 50], [200, 150], [100, 150]]],
        "changed_share": made["changed_share"],
    }
    assert str(book) not in json.dumps(data)
    assert client.get(composite["url"]).mimetype == "image/png"

    saved = _post(client, {"image": "map.jpg",
                           "pick": {"verdict": "accept", "candidate": made["candidate"]}})
    assert saved.status_code == 200 and saved.get_json()["image"]["pick"]["pending"] is True


def test_a_replaced_image_shows_the_publishers_file_from_the_backup(client, book):
    before = (book / "images" / "map.jpg").read_bytes()
    assert ip_apply.apply(book, [{"image": "map.jpg", "candidate": 1}])["counts"]["applied"] == 1

    row = _rows(client)[0]["map.jpg"]
    assert row["stage"] == "replaced"
    original, current = row["pictures"]
    assert original["url"].startswith("/projects/imgbook/image-pass/original/map.jpg?v=")
    assert client.get(original["url"]).data == before
    assert client.get(current["url"]).data == (book / "images" / "map.jpg").read_bytes() != before


def test_the_picture_routes_stay_inside_their_folders(client, book):
    (book / "images_original").mkdir()
    (book / "secret.txt").write_text("no", "utf-8")
    assert client.get("/projects/imgbook/image-pass/original/../secret.txt").status_code == 404
    assert client.get("/projects/imgbook/image-pass/original/%2e%2e/secret.txt").status_code == 404
    assert client.get("/projects/imgbook/image-pass/candidate/map.jpg/9").status_code == 404
    assert client.get("/projects/imgbook/image-pass/candidate/nojob/1").status_code == 404
    assert client.get("/projects/imgbook/image-pass/candidate/../1").status_code in (400, 404)


# --- what the page writes ---------------------------------------------------

def test_a_request_lands_on_disk_and_comes_back_on_the_row(client, book):
    reply = _post(client, {"image": "plates/dial.png", "request": {
        "verdict": "restore", "note": "Clean the foxing.", "candidates": 2, "labels": None,
    }})
    assert reply.status_code == 200
    body = reply.get_json()
    assert body["saved"] is True and body["counts"]["with_input"] == 1
    # Triage never saw it; the user's verdict is what moves it out of the queue.
    assert body["image"]["stage"] == "proposed" and body["image"]["drift"] == ["needs_job"]

    on_disk = ip_feedback.load(book)["plates/dial.png"]["request"]
    assert (on_disk["verdict"], on_disk["note"], on_disk["candidates"]) == (
        "restore", "Clean the foxing.", 2,
    )
    assert ip_board.summary(book)["requests"][0]["image"] == "plates/dial.png"


def test_the_two_halves_of_a_card_save_independently(client, book):
    assert _post(client, {"image": "map.jpg", "request": {
        "labels": [["NORTH", "NORTE"], ["EAST", "ESTE"]],
    }}).status_code == 200
    reply = _post(client, {"image": "map.jpg", "pick": {
        "verdict": "accept", "candidate": 1, "note": "Lettering is right.",
    }})
    row = reply.get_json()["image"]
    assert row["pick"]["pending"] is True and row["pick"]["stale"] is False
    # The pick did not take the label edit with it, and the edit shows as drift.
    assert row["labels"] == [["NORTH", "NORTE"], ["EAST", "ESTE"]]
    assert row["drift"] == ["labels_differ"]

    decisions, stale = ip_board.decisions_from_picks(ip_board.build(book))
    assert stale == [] and decisions == [
        {"image": "map.jpg", "note": "Lettering is right.", "candidate": 1},
    ]

    # null clears one half and leaves the other.
    row = _post(client, {"image": "map.jpg", "pick": None}).get_json()["image"]
    assert row["pick"] is None and row["request"]["labels"]["EAST"] == "ESTE"
    row = _post(client, {"image": "map.jpg", "request": None}).get_json()["image"]
    assert row["request"] is None and row["labels"] == [["NORTH", "NORTE"]]
    assert ip_feedback.load(book) == {}


def test_a_label_map_keeps_the_order_it_was_listed_in(client, book):
    """An object would not: `jsonify` sorts keys, and a browser puts "90" first.
    The lettering is listed in the order it is read off the picture."""
    listed = [["W", "O"], ["90", "90"], ["NW", "NO"], ["E", "E"]]
    row = _post(client, {"image": "map.jpg", "request": {"labels": listed}}).get_json()["image"]
    assert row["labels"] == listed
    assert _rows(client)[0]["map.jpg"]["labels"] == listed
    assert list(ip_feedback.load(book)["map.jpg"]["request"]["labels"]) == ["W", "90", "NW", "E"]


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"image": "map.jpg", "request": {"verdict": "polish"}}, 400),
        ({"image": "map.jpg", "request": {"labels": {"NORTH": ""}}}, 400),
        ({"image": "map.jpg", "request": {"labels": [["NORTH"]]}}, 400),
        ({"image": "map.jpg", "pick": {"verdict": "accept", "candidate": 7}}, 400),
        ({"image": "plates/dial.png", "pick": {"verdict": "skip"}}, 400),
        ({"image": "stray.jpg", "request": {"note": "x"}}, 404),
        ({"image": "../project.json", "request": {"note": "x"}}, 404),
        ({"request": {"note": "x"}}, 400),
    ],
)
def test_a_bad_save_is_refused_and_writes_nothing(client, book, payload, status):
    reply = _post(client, payload)
    assert reply.status_code == status and reply.get_json()["error"]
    assert not ip_feedback.feedback_path(book).exists()


# --- the cover's workspace --------------------------------------------------

@pytest.fixture
def cover(book):
    """What ``cover-compose`` leaves in ``cover/``: the art, a proof with its
    draft, and a sheet somebody made by hand. Returns the draft's row."""
    folder = book / "cover"
    art = _image(folder / "art.png", (400, 600), (20, 160, 60))
    _image(folder / "proof.png", (800, 1200))
    _image(folder / "variants.png", (1600, 600))
    row, _new = ip_drafts.record(
        book, Image.new("RGB", (800, 1200), (90, 40, 20)), kind="proof",
        spec={"title": "Img Book", "colors": {"band": "#703010"}},
        art_sha=ledger.sha256_file(art),
    )
    (folder / "spec.json").write_text(json.dumps({
        "title": "Img Book", "colors": {"band": "#703010"},
        "state": {"proof_draft": row["n"]},
    }), "utf-8")
    return row


def _workspace_pictures(workspace):
    for step in workspace["steps"]:
        yield from step["pictures"]
        for group in step["more"]:
            yield from group["pictures"]


def test_the_two_tabs_are_counted_apart_and_the_cover_is_a_workspace(client, book, cover):
    rows, data = _rows(client)
    tracks = data["counts"]["tracks"]
    assert tracks["existing"]["images"] == 2 and tracks["existing"]["review"] == 1
    assert tracks["new"]["images"] == 1 and tracks["existing"]["waiting"] == 1
    assert rows["map.jpg"]["track"] == "existing" and rows["map.jpg"]["workspace"] is None

    row = rows["cover.jpg"]
    workspace = row["workspace"]
    assert row["track"] == "new" and workspace["status"] == "approve"
    assert [step["id"] for step in workspace["steps"]] == ["art", "colour", "layout", "book"]
    # Where the proof is on disk is for the agent's summary, not for the page.
    assert "proof" not in workspace
    pictures = [workspace["hero"], *_workspace_pictures(workspace)]
    assert {picture["kind"] for picture in pictures} == {"art", "draft", "sheet"}
    for picture in pictures:
        assert "path" not in picture
        served = client.get(picture["url"])
        assert served.status_code == 200 and served.mimetype.startswith("image/"), picture
    assert workspace["hero"]["url"].startswith("/projects/imgbook/image-pass/cover/drafts/001.jpg?v=")


def test_candidates_of_an_earlier_prompt_are_served_from_where_they_were_moved(client, book):
    ip_jobs.prepare(book, [{
        "image": "map.jpg", "mode": "translate", "instruction": "Thinner strokes.",
        "labels": {"NORTH": "NORTE"},
    }])
    (earlier,) = _rows(client)[0]["map.jpg"]["earlier"]
    assert "/image-pass/earlier/map.jpg/" in earlier["url"] and "path" not in earlier
    served = client.get(earlier["url"])
    assert served.status_code == 200 and served.mimetype == "image/png"


def test_the_cover_routes_serve_pictures_and_nothing_else(client, book, cover):
    base = "/projects/imgbook/image-pass"
    assert client.get(f"{base}/cover/drafts/001.jpg").status_code == 200
    # The spec and the drafts' record are in that folder too.
    assert client.get(f"{base}/cover/spec.json").status_code == 404
    assert client.get(f"{base}/cover/drafts.json").status_code == 404
    assert client.get(f"{base}/cover/../images/map.jpg").status_code == 404
    assert client.get(f"{base}/cover/%2e%2e/images/map.jpg").status_code == 404
    assert client.get(f"{base}/cover/nothing.png").status_code == 404
    assert client.get(f"{base}/earlier/map.jpg/not-a-stamp/1").status_code == 400
    assert client.get(f"{base}/earlier/map.jpg/20261005_182844/1").status_code == 404


def test_a_design_lands_on_disk_tied_to_the_draft_it_was_made_on(client, book, cover):
    reply = _post(client, {"image": "cover.jpg", "design": {
        "draft": cover["n"], "approve": True, "note": "Title a little larger",
    }})
    assert reply.status_code == 200, reply.get_json()
    design = reply.get_json()["image"]["workspace"]["design"]
    assert design["draft"] == cover["n"] and design["approve"] and not design["stale"]
    assert design["waiting"] == ["not_applied"]
    on_disk = ip_feedback.load(book)["cover.jpg"]["design"]
    assert on_disk["id"] == cover["id"] and on_disk["note"] == "Title a little larger"
    assert reply.get_json()["counts"]["tracks"]["new"]["with_input"] == 1

    # A request on the same image is its own section: neither drops the other.
    _post(client, {"image": "cover.jpg", "request": {"note": "More sky"}})
    said = ip_feedback.load(book)["cover.jpg"]
    assert said["design"]["draft"] == cover["n"] and said["request"]["note"] == "More sky"

    # Nothing chosen and no note is no design at all.
    _post(client, {"image": "cover.jpg", "design": {"draft": None, "approve": False, "note": ""}})
    assert "design" not in ip_feedback.load(book)["cover.jpg"]


@pytest.mark.parametrize("payload", [
    {"image": "cover.jpg", "design": {"draft": 9}},
    {"image": "cover.jpg", "design": {"approve": True}},
    {"image": "cover.jpg", "design": {"draft": 1, "approve": "yes"}},
    {"image": "map.jpg", "design": {"draft": 1}},
])
def test_a_design_that_names_no_real_draft_is_refused(client, book, cover, payload):
    reply = _post(client, payload)
    assert reply.status_code == 400 and reply.get_json()["error"]
    assert not ip_feedback.feedback_path(book).exists()
