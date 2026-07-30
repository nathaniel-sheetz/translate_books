"""Bracket-anchor parsing across the shapes real books actually contain."""

from __future__ import annotations

import pytest

from src.annotations.anchors import bare_hint, is_effectively_blank, parse_anchors


@pytest.mark.parametrize(
    "content, expected",
    [
        # What the reader seeds today: tapped word in front.
        ("[muserón] ¿muserola?", ["muserón"]),
        # Migrated rows put the bracket last (scripts/migrate_annotations.py).
        ('"la ficción es irrelevante" [nadería;]', ["nadería;"]),
        # One note standing in for several glosses.
        ("[Neuve-Celle,]; [Esaú,]; [Montélimar.]", ["Neuve-Celle,", "Esaú,", "Montélimar."]),
        # Bare word, no bracket at all.
        ("humilde", []),
        ("", []),
        # Punctuation stays inside the anchor — endnotes matches it verbatim.
        ("[by then,] note", ["by then,"]),
        # Empty brackets carry no anchor.
        ("[] something", []),
    ],
)
def test_parse_anchors(content, expected):
    assert parse_anchors(content) == expected


@pytest.mark.parametrize(
    "content, expected",
    [
        ("[muserón] ¿muserola?", "¿muserola?"),
        ("[Sancerre]", ""),
        ("humilde", "humilde"),
        ("", ""),
        ('"la ficción es irrelevante" [nadería;]', '"la ficción es irrelevante"'),
        # Only the separators survive: the note is three anchors and nothing else.
        ("[Neuve-Celle,]; [Esaú,]; [Montélimar.]", "; ;"),
        # Whitespace is collapsed so a multi-line note reads as one hint.
        ("error?\n...comiencen a crecer. [en]", "error? ...comiencen a crecer."),
    ],
)
def test_bare_hint(content, expected):
    assert bare_hint(content) == expected


@pytest.mark.parametrize(
    "content, blank",
    [
        ("", True),
        ("[Sancerre]", True),
        ("   ", True),
        ("[Sancerre] Ciudad de Francia.", False),
        ("biblia", False),
    ],
)
def test_is_effectively_blank(content, blank):
    assert is_effectively_blank(content) is blank
