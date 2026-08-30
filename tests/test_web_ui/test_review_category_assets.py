"""Every review category must be wired into the reader's static assets.

The server side of a new judge is generic — :data:`REVIEW_TYPES` drives the
endpoint, the counts and the picker — so a category can persist findings, badge
them on the dashboard, and still be invisible in the reader because nothing
paints ``.review-editorial``. That is exactly how the editorial judge shipped.
These tests pin the two files that do not import the constant.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from web_ui.evaluations import REVIEW_TYPES

STATIC = Path(__file__).resolve().parents[2] / "web_ui" / "static"
READER_CSS = (STATIC / "reader.css").read_text(encoding="utf-8")
READER_JS = (STATIC / "reader.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("category", REVIEW_TYPES)
@pytest.mark.parametrize(
    "template",
    [
        "--review-{c}:",              # light-theme accent + tint
        ".review-type-{c} span::before",   # picker's color dot
        ".review-hl.review-{c}",          # word-level highlight
        ".sentence.review-flagged.review-{c}",  # whole-sentence fallback tint
        ".review-item-{c}",               # finding card's left border
        ".review-item-type.review-{c}",   # finding card's category chip
    ],
)
def test_reader_css_styles_every_category(category, template):
    assert template.format(c=category) in READER_CSS


@pytest.mark.parametrize("category", REVIEW_TYPES)
def test_reader_css_has_a_dark_theme_accent(category):
    dark = READER_CSS.split('[data-theme="dark"]', 1)[1]
    assert f"--review-{category}:" in dark


def test_reader_js_fallback_list_matches_review_types():
    """The list used when the server did not render ``window.REVIEW_TYPES``."""
    match = re.search(
        r"const REVIEW_TYPES = window\.REVIEW_TYPES \|\|\s*\[(.*?)\];",
        READER_JS,
        re.S,
    )
    assert match, "reader.js no longer declares a REVIEW_TYPES fallback"
    fallback = re.findall(r"'([a-z_]+)'", match.group(1))
    assert fallback == list(REVIEW_TYPES)
