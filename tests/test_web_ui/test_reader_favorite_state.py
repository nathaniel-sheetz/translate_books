"""The reader's heart has to survive closing and reopening the sheet.

The sheet rebuilds its cards from scratch on every open — `renderAnnotate`
reads `ann.favorite` off the row, `renderIssues` reads `f.favorite` — and the
rows are `reader.js`'s own `annotationsMap` / `reviewMap`, loaded once per
chapter. The first cut of the heart flipped only `aria-pressed` on the button,
so the mark reached the server but never the row it was drawn from: close the
drawer, open it again, and the heart came back unlit. Worse than cosmetic —
the next tap read the unlit button as "not favorited" and re-sent
`favorite: true`, which is how a favorites.jsonl ends up with two identical
records for one note and no way to unfavorite it from the reader.

JS has no runner here, so the wiring is asserted by reading the source and the
helper's semantics by mirroring it (the pattern in test_reader_topbar_nav.py).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "web_ui" / "static"
READER_JS = (STATIC / "reader.js").read_text(encoding="utf-8")
SHEET_JS = (STATIC / "reader_sheet_v2.js").read_text(encoding="utf-8")


class TestTheRowMovesWithTheButton:
    def test_reader_js_exposes_the_stamp(self):
        assert "function stampFavorite(favId, on)" in READER_JS

    def test_the_stamp_walks_both_caches(self):
        """A note lives in one map and a finding in the other; the heart is the
        same control on both tabs, so one helper has to reach both."""
        body = _fn_body(READER_JS, "stampFavorite")
        assert "annotationsMap" in body and "reviewMap" in body
        assert "rec.favorite = on" in body

    def test_the_stamp_matches_on_fav_id_not_position(self):
        """Held record references would go stale: saving an edit replaces the
        object in `annotationsMap` (see `applySaved`), and a refusal arriving
        after that has to find the new one."""
        assert "rec.fav_id === favId" in _fn_body(READER_JS, "stampFavorite")

    def test_the_row_is_stamped_before_the_request_goes_out(self):
        """Optimistic on the row for the same reason it is optimistic on the
        button: the sheet can be closed and reopened inside the round trip."""
        body = _fn_body(READER_JS, "setFavorite")
        stamp = body.index("stampFavorite(favId, !!on);")
        assert stamp < body.index("fetch(url, {")

    def test_a_refusal_puts_the_row_back_too(self):
        """`onReject` only repaints buttons, and only ones still in the DOM."""
        body = _fn_body(READER_JS, "setFavorite")
        assert re.search(
            r"if \(!r\.ok\) \{\s*stampFavorite\(favId, !on\);\s*if \(onReject\) onReject\(\);",
            body,
        ), "the !r.ok branch no longer reverts the row"

    def test_going_offline_keeps_the_mark(self):
        """The catch queues the write and leaves the heart lit, so the row it
        was drawn from must stay stamped as well — no revert in that branch."""
        body = _fn_body(READER_JS, "setFavorite")
        catch = body[body.index(".catch("):]
        assert "stampFavorite" not in catch
        assert "enqueue(url, 'POST', payload)" in catch


class TestTheSheetStillReadsTheRow:
    """What makes the stamp load-bearing: if the sheet ever kept its own copy of
    the state these assertions should fail and this whole file be rethought."""

    def test_cards_are_rebuilt_from_the_rows_on_every_open(self):
        assert "function onOpen(data)" in SHEET_JS
        onopen = _fn_body(SHEET_JS, "onOpen")
        assert "cur = data;" in onopen
        assert "renderAnnotate();" in onopen and "renderIssues();" in onopen

    def test_the_heart_is_drawn_from_the_rows_own_flag(self):
        assert "favButton(ann.fav_id, ann.favorite" in SHEET_JS
        assert "favButton(f.fav_id, f.favorite" in SHEET_JS

    def test_the_sheet_composes_no_state_of_its_own(self):
        """The sheet paints buttons; the row is reader.js's to own."""
        assert ".favorite =" not in SHEET_JS


def stamp_favorite(maps, fav_id, on):
    """Mirror of reader.js ``stampFavorite`` — keep in lockstep with it."""
    if not fav_id:
        return
    for m in maps:
        for idx in list(m):
            for rec in m.get(idx) or []:
                if rec.get("fav_id") == fav_id:
                    rec["favorite"] = on


class TestStampSemantics:
    @pytest.fixture
    def maps(self):
        # The third row has no `fav_id` at all, which is what a note created
        # while offline looks like until a reload names it (see `applySaved`).
        anns = {"4": [{"fav_id": "annotation:c__4__u1", "favorite": False},
                      {"fav_id": "annotation:c__4__u2", "favorite": False},
                      {"favorite": False}]}
        # One dictionary hit on a term used twice: two locations, two rows in
        # two sentences, one issue_key — so one fav_id.
        findings = {"4": [{"fav_id": "finding:c_chunk_000:ab12", "favorite": False}],
                    "9": [{"fav_id": "finding:c_chunk_000:ab12", "favorite": False}]}
        return [anns, findings]

    def test_the_hearted_note_is_the_only_note_that_moves(self, maps):
        stamp_favorite(maps, "annotation:c__4__u1", True)
        assert [a["favorite"] for a in maps[0]["4"]] == [True, False, False]

    def test_every_row_of_one_finding_moves_together(self, maps):
        """Including the rows under another es_idx, which `setFavAll` cannot see:
        it only repaints buttons inside the open sheet. Opening sentence 9 next
        must not show an unlit heart on a mark that is saved."""
        stamp_favorite(maps, "finding:c_chunk_000:ab12", True)
        assert all(f["favorite"] for group in maps[1].values() for f in group)

    def test_unfavoriting_moves_the_same_set_back(self, maps):
        stamp_favorite(maps, "finding:c_chunk_000:ab12", True)
        stamp_favorite(maps, "finding:c_chunk_000:ab12", False)
        assert not any(f["favorite"] for group in maps[1].values() for f in group)

    @pytest.mark.parametrize("missing", [None, "", 0])
    def test_an_id_nothing_carries_changes_nothing(self, maps, missing):
        """A finding with no chunk_id gets `fav_id: None` and no heart. Without
        a guard this is the dangerous call, not a harmless one: the rows that
        carry no `fav_id` are exactly the ones a falsy id would match, and
        `undefined === undefined` in JS is as true as `None == None` here.
        """
        stamp_favorite(maps, missing, True)
        assert not any(r["favorite"] for m in maps for g in m.values() for r in g)

    def test_the_guard_is_in_the_real_source_too(self):
        """The mirror above is only evidence if the JS has the same guard."""
        assert "if (!favId) return;" in _fn_body(READER_JS, "stampFavorite")


def _fn_body(source, name):
    """The text of a named JS function, brace-matched from its declaration.

    Reading a function whole beats grepping the file for a line: an assertion
    about where `stampFavorite` sits relative to `fetch` is only meaningful
    inside the function that does both.
    """
    match = re.search(r"(?:function\s+" + name + r"\s*\(|\b" + name + r"\s*\([^)]*\)\s*\{)",
                      source)
    assert match, f"{name} is gone from the source"
    start = source.index("{", match.start())
    depth = 0
    for pos in range(start, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start:pos + 1]
    raise AssertionError(f"unbalanced braces reading {name}")
