"""The address judge must see the map's pairs and global rules, not only its prose."""

from pathlib import Path

from src.judges.address_judge import render_address_map
from src.judges.context import build_judge_context
from src.models import AddressMap, AddressPair, AddressRule
from src.utils.file_io import save_address_map


def _map() -> AddressMap:
    return AddressMap(
        content="tú DOWN (to servants and to children), usted UP.",
        global_rules="adults to children: tú, except where a named pair says usted.",
        pairs=[
            AddressPair(
                a="Pollyanna",
                b="Nancy",
                relationship="the niece and the housemaid",
                directions={
                    "a_to_b": [AddressRule(form="tú", notes="Warm.")],
                    "b_to_a": [AddressRule(form="usted", notes="Servant to the young mistress.")],
                },
            ),
            AddressPair(
                a="Pollyanna",
                b="Dr. Chilton",
                directions={
                    "b_to_a": [
                        AddressRule(
                            form="tú",
                            when="once he is family-to-be",
                            since="chapter_31",
                            after_event="the reunion",
                        ),
                        AddressRule(form="usted"),
                    ],
                },
            ),
        ],
    )


def test_render_includes_prose_global_rules_and_each_direction():
    text = render_address_map(_map())
    assert text.startswith("tú DOWN")
    assert "GLOBAL RULES" in text and "except where a named pair says usted" in text
    assert "- Pollyanna → Nancy (the niece and the housemaid): tú [default] — Warm." in text
    assert "- Nancy → Pollyanna (the niece and the housemaid): usted [default] — Servant" in text
    # A direction with no rules is not rendered.
    assert "Pollyanna → Dr. Chilton" not in text


def test_render_keeps_rule_order_and_chapter_window():
    text = render_address_map(_map())
    line = next(l for l in text.splitlines() if l.startswith("- Dr. Chilton → Pollyanna"))
    assert line.endswith(
        "tú [once he is family-to-be, from chapter_31, after: the reunion]; usted [default]"
    )


def test_render_of_a_prose_only_map_is_the_prose():
    assert render_address_map(AddressMap(content="Only prose.")) == "Only prose."


def test_judge_context_carries_pairs_not_only_content(tmp_path: Path):
    save_address_map(_map(), tmp_path / "address_map.json")
    context, error = build_judge_context(tmp_path, ["address"], None, None)
    assert error is None
    assert "Nancy → Pollyanna" in context["address_map"]
    assert "usted [default]" in context["address_map"]
