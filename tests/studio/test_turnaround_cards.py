"""Placement cards and synthesized parts follow the direction geometry table."""

import pytest

from magic_video_editor.studio.models import TurnaroundEquipment, ViewId
from magic_video_editor.studio.turnaround_cards import (
    body_side, cards, placement_card, side_location, synthesized_parts,
)

# The design's table, verbatim: where the character's right and left land.
TABLE = {
    ViewId.N: ("right of picture", "left of picture"),
    ViewId.NE: ("right of picture, near", "left of picture, far"),
    ViewId.E: ("facing the camera", "far side, behind the body"),
    ViewId.SE: ("left of picture, near", "right of picture, far"),
    ViewId.S: ("left of picture", "right of picture"),
    ViewId.SW: ("left of picture, far", "right of picture, near"),
    ViewId.W: ("far side, behind the body", "facing the camera"),
    ViewId.NW: ("right of picture, far", "left of picture, near"),
}
AXE = TurnaroundEquipment(item="axe", attachment="hand", source_picture_side="left")
PAULDRON = TurnaroundEquipment(item="pauldron", attachment="shoulder", source_picture_side="right")
QUIVER = TurnaroundEquipment(item="quiver", attachment="back", source_picture_side="unsided")


@pytest.mark.parametrize("heading", list(TABLE))
def test_every_heading_places_both_sides_as_the_table_says(heading):
    assert (side_location(heading, "right"), side_location(heading, "left")) == TABLE[heading]


@pytest.mark.parametrize("source,picture,body", [
    (ViewId.S, "left", "right"), (ViewId.S, "right", "left"),
    (ViewId.SE, "left", "right"), (ViewId.SW, "left", "right"),
    (ViewId.N, "left", "left"), (ViewId.E, "left", None), (ViewId.S, "unsided", None),
])
def test_a_picture_side_becomes_the_characters_side_from_the_source_heading(source, picture, body):
    assert body_side(source, picture) == body


@pytest.mark.parametrize("target,expected", [
    (ViewId.SE, ["Facing front-right.", "Axe in the near hand.", "Pauldron on the far shoulder.",
                 "Quiver on the back, behind the body."]),
    (ViewId.W, ["Left profile.", "Axe in the far hand.", "Pauldron toward you.",
                "Quiver on the back, seen from the side."]),
    (ViewId.E, ["Right profile.", "Axe toward you.", "Pauldron on the far shoulder.",
                "Quiver on the back, seen from the side."]),
    (ViewId.N, ["Back to you.", "Axe in the hand on the picture's right.",
                "Pauldron on the shoulder on the picture's left.", "Quiver on the back, in view."]),
    (ViewId.NW, ["Facing back-left.", "Axe in the far hand.", "Pauldron on the near shoulder.",
                 "Quiver on the back, in view."]),
])
def test_a_card_says_plainly_what_a_correct_picture_shows(target, expected):
    # The orc: axe in the right hand (picture left of the S source), pauldron on the left shoulder.
    assert placement_card(ViewId.S, target, (AXE, PAULDRON, QUIVER)) == expected


def test_an_unsided_item_gets_no_side_claim():
    north = placement_card(ViewId.S, ViewId.N, (QUIVER, TurnaroundEquipment(
        item="banner", attachment="other", source_picture_side="unsided")))
    assert north[1:] == ["Quiver on the back, in view.", "Banner where the source shows it."]
    assert not any(word in " ".join(north[1:]) for word in ("left", "right", "near", "far"))


def test_synthesized_parts_are_what_the_source_never_showed():
    assert synthesized_parts(ViewId.S, ViewId.N) == [
        "the back of the head, hair and outfit, and equipment on the back"]
    assert synthesized_parts(ViewId.S, ViewId.E) == ["the character's right side"]
    assert synthesized_parts(ViewId.SE, ViewId.E) == []
    assert synthesized_parts(ViewId.S, ViewId.NW) == [
        "the back of the head, hair and outfit, and equipment on the back", "the character's left side"]


def test_cards_cover_every_heading_but_the_source():
    result = cards(ViewId.S, (AXE,))
    assert set(result["placement_cards"]) == set(result["synthesized_parts"]) == {
        "N", "NE", "E", "SE", "SW", "W", "NW"}
    assert result["cards_version"] == "placement_cards.v2"


@pytest.mark.parametrize("target,expected", [
    (ViewId.S, "Sword on the back, over the shoulder on the picture's right."),
    (ViewId.N, "Sword on the back, over the shoulder on the picture's left."),
    (ViewId.W, "Sword on the back, over the shoulder toward you."),
    (ViewId.E, "Sword on the back, over the far shoulder."),
    (ViewId.NE, "Sword on the back, over the far shoulder."),
])
def test_a_sided_back_item_keeps_its_side_in_every_view(target, expected):
    # Hilt over the left shoulder: the picture's right of an S source.
    sword = TurnaroundEquipment(item="sword", attachment="back", source_picture_side="right")
    assert placement_card(ViewId.S, target, (sword,))[1] == expected


def test_back_items_first_seen_side_on_are_synthesized():
    assert synthesized_parts(ViewId.S, ViewId.W, (AXE, QUIVER)) == [
        "the character's left side", "the quiver on the back"]
    # Where the whole back is new, its line already covers the gear on it.
    assert synthesized_parts(ViewId.S, ViewId.N, (QUIVER,)) == [
        "the back of the head, hair and outfit, and equipment on the back"]
    assert synthesized_parts(ViewId.S, ViewId.SE, (QUIVER,)) == ["the character's right side"]
    assert cards(ViewId.S, (QUIVER,))["synthesized_parts"]["E"] == [
        "the character's right side", "the quiver on the back"]


def test_a_sided_back_item_was_seen_in_the_source_so_it_is_not_synthesized():
    sword = TurnaroundEquipment(item="sword", attachment="back", source_picture_side="right")
    assert synthesized_parts(ViewId.S, ViewId.E, (sword, QUIVER)) == [
        "the character's right side", "the quiver on the back"]
