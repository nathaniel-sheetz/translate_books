"""The bookmark has to find its sentence again, and keep finding it.

`bookmark.json` names a sentence by number and by its opening words. A realign
renumbers, so the words decide — but dialogue is full of sentences that open
alike ("—No."), and the first cut took the first of them in the chapter: a
bookmark on row 250, pushed to 251 by a split upstream, landed on row 12, was
painted there, and the next Save on that row made the wrong place permanent.
The number keeps its say now: of several rows that open alike, the closest one.

The other three tests hold the places where the stored words or the stored
write could go stale: two sets queued offline, and a Retranslate → Replace of
the bookmarked sentence.

JS has no runner here, so the wiring is asserted by reading the source and the
lookup's semantics by mirroring it (the pattern in test_reader_favorite_state.py).
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "web_ui" / "static"
READER_JS = (STATIC / "reader.js").read_text(encoding="utf-8")


def find_by_anchor(rows, prefix, esi, near, rendered=None):
    """Mirror of reader.js ``findByAnchor`` — keep in lockstep with it.

    ``rendered`` stands in for the `[data-es-idx]` elements on the page; it
    defaults to every row.
    """
    want = None if esi in (None, "") else str(esi)
    target = float(want) if (near and want is not None) else math.nan
    if prefix:
        if want is not None:
            for a in rows:
                if str(a["es_idx"]) == want and a["es"].startswith(prefix):
                    return a
        best, best_gap = None, math.inf
        for a in rows:
            if not a["es"].startswith(prefix):
                continue
            if not math.isfinite(target):
                return a
            gap = abs(a["es_idx"] - target)
            if best is None or gap < best_gap:
                best, best_gap = a, gap
        if best:
            return best
    if not near or want is None or not math.isfinite(target):
        return None
    on_page = sorted(rendered if rendered is not None else [a["es_idx"] for a in rows])
    if not on_page:
        return None
    below = [n for n in on_page if n <= target]
    idx = below[-1] if below else on_page[0]
    return next((a for a in rows if a["es_idx"] == idx), None)


class TestWhichRowTheBookmarkNames:
    @pytest.fixture
    def rows(self):
        # Row 250 was "—No." when it was bookmarked; a split upstream has since
        # pushed that sentence to 251, and row 12 has always opened the same way.
        text = {12: "—No. No lo haré.", 249: "Ella lo miró.", 250: "Guardó silencio.",
                251: "—No.", 252: "Y se fue."}
        return [{"es_idx": n, "es": text[n]} for n in sorted(text)]

    def test_the_closest_of_several_alike_wins(self, rows):
        assert find_by_anchor(rows, "—No.", 250, True)["es_idx"] == 251

    def test_other_deep_links_still_take_the_first(self, rows):
        """Search and recommendations links carry no `near`; theirs is the
        first-match rule they shipped with."""
        assert find_by_anchor(rows, "—No.", 250, False)["es_idx"] == 12

    def test_the_numbered_row_wins_while_it_still_opens_that_way(self, rows):
        assert find_by_anchor(rows, "—No.", 12, True)["es_idx"] == 12
        assert find_by_anchor(rows, "—No.", 251, False)["es_idx"] == 251

    def test_words_found_nowhere_fall_back_on_the_number(self, rows):
        assert find_by_anchor(rows, "Reescrita.", 250, True)["es_idx"] == 250
        # ...or the closest row before it that is on the page.
        assert find_by_anchor(rows, "Reescrita.", 250, True,
                              rendered=[12, 249, 251, 252])["es_idx"] == 249

    def test_without_near_words_found_nowhere_name_no_row(self, rows):
        assert find_by_anchor(rows, "Reescrita.", 250, False) is None

    def test_the_source_measures_the_gap_only_under_near(self):
        body = _fn_body(READER_JS, "findByAnchor")
        assert "const target = (near && want !== null) ? Number(want) : NaN;" in body
        assert "if (!Number.isFinite(target)) return a;" in body
        assert "Math.abs(n - target)" in body

    def test_the_bookmark_resolves_with_near(self):
        assert re.search(r"findByAnchor\([^;]*bookmark\.es_idx, true\)",
                         _fn_body(READER_JS, "resolveBookmark"))


class TestOneQueuedWriteAtATime:
    def test_a_queued_bookmark_write_replaces_the_earlier_one(self):
        """`flushQueue` replays every item at once. Two sets made offline would
        land in no order, so the catch drops the earlier one before it queues."""
        body = _fn_body(READER_JS, "setBookmark")
        catch = body[body.index(".catch("):]
        drop = catch.index("getQueue().filter(item => item.url !== BOOKMARK_URL)")
        store = catch.index("localStorage.setItem(QUEUE_KEY, JSON.stringify(kept));")
        assert drop < store < catch.index("enqueue(BOOKMARK_URL, method, payload);")


class TestReplaceCarriesTheBookmark:
    """A Save re-sends the bookmark with the new wording; Retranslate → Replace
    reloads the chapter instead, so it has to do the same after the reload."""

    @pytest.fixture
    def handler(self):
        start = READER_JS.index("const wasBookmarked =")
        return READER_JS[start:READER_JS.index("toastSaveCheck(body.check);", start)]

    def test_it_is_read_before_the_modal_forgets_the_row(self, handler):
        """`closeRetransModal` nulls `retransCtx`."""
        assert "retransCtx.row.es_idx === bookmarkIdx" in handler
        assert handler.index("bookmarkIdx") < handler.index("closeRetransModal();")

    def test_the_new_wording_is_sent_once_the_reload_has_the_row(self, handler):
        resend = handler[handler.index("if (wasBookmarked) {"):]
        assert handler.index("const reloaded = loadAndRender(scrollAnchor);") < \
            handler.index("if (wasBookmarked) {")
        assert "reloaded.then(() => {" in resend
        assert "a.es_idx === bookmarkIdx" in resend
        assert "setBookmark(row, true);" in resend


def _fn_body(source, name):
    """The text of a named JS function, brace-matched from its declaration."""
    match = re.search(r"function\s+" + name + r"\s*\(", source)
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
