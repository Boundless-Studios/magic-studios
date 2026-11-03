"""Placement cards and synthesized parts, computed from the direction geometry.

Nothing here is hand-written per heading: every sentence comes from one table
(design "Geometry: where each side lands"). Screen-forward vectors put x to the
picture's right and y away from the viewer; the character's right is
(f_y, -f_x). A diagonal means the supplied projection, not a literal 45 degrees.
"""

from __future__ import annotations

from typing import Any

from .models import TurnaroundEquipment, ViewId

CARDS_VERSION = "placement_cards.v2"

FORWARD = {
    ViewId.N: (0, 1), ViewId.NE: (1, 1), ViewId.E: (1, 0), ViewId.SE: (1, -1),
    ViewId.S: (0, -1), ViewId.SW: (-1, -1), ViewId.W: (-1, 0), ViewId.NW: (-1, 1),
}
# Which of the body's four faces each heading shows.
_COVERAGE = {
    ViewId.S: {"front"}, ViewId.SE: {"front", "right"}, ViewId.E: {"right"}, ViewId.NE: {"back", "right"},
    ViewId.N: {"back"}, ViewId.NW: {"back", "left"}, ViewId.W: {"left"}, ViewId.SW: {"front", "left"},
}
_PARTS = (
    ("front", "the face and the front of the outfit"),
    ("back", "the back of the head, hair and outfit, and equipment on the back"),
    ("right", "the character's right side"),
    ("left", "the character's left side"),
)
_ATTACHMENT_NOUN = {"hand": "hand", "shoulder": "shoulder", "hip": "hip"}
# How a correct picture at each heading faces, as the card's first sentence.
FACING = {
    ViewId.S: "Facing you", ViewId.SE: "Facing front-right", ViewId.E: "Right profile",
    ViewId.NE: "Facing back-right", ViewId.N: "Back to you", ViewId.NW: "Facing back-left",
    ViewId.W: "Left profile", ViewId.SW: "Facing front-left",
}


def body_side(source: ViewId, picture_side: str) -> str | None:
    """The character's side for an item seen on `picture_side` of the source.

    None when it cannot be told: an unsided item, or a profile source, where
    both sides sit on the same picture side (near and far).
    """
    if picture_side not in ("left", "right"):
        return None
    forward_y = FORWARD[source][1]
    if forward_y == 0:
        return None
    # The character's right lies at picture x = f_y: on the picture's left when
    # the character faces the viewer, on its right when facing away.
    right_on_picture = "left" if forward_y < 0 else "right"
    return "right" if picture_side == right_on_picture else "left"


def _position(heading: ViewId, side: str) -> tuple[int, int]:
    """The character's `side` as a screen vector at `heading`: x right, y away."""
    fx, fy = FORWARD[heading]
    return (fy, -fx) if side == "right" else (-fy, fx)


def side_location(heading: ViewId, side: str) -> str:
    """Where the character's `side` appears in a picture at `heading` (the design's table)."""
    x, y = _position(heading, side)
    if x == 0:
        return "facing the camera" if y < 0 else "far side, behind the body"
    where = "right of picture" if x > 0 else "left of picture"
    if y == 0:
        return where
    return f"{where}, {'near' if y < 0 else 'far'}"


def _held(heading: ViewId, side: str, attachment: str) -> str:
    """Where a one-sided item sits in a `heading` picture, in the viewer's words."""
    x, y = _position(heading, side)
    noun = _ATTACHMENT_NOUN[attachment]
    on = "in" if attachment == "hand" else "on"
    if x == 0 and y < 0:
        return "toward you"
    if y == 0:
        return f"{on} the {noun} on the picture's {'right' if x > 0 else 'left'}"
    return f"{on} the {'near' if y < 0 else 'far'} {noun}"


def _over(heading: ViewId, side: str) -> str:
    """Which shoulder a sided back item shows over in a `heading` picture."""
    x, y = _position(heading, side)
    if x == 0:
        return "over the shoulder toward you" if y < 0 else "over the far shoulder"
    if y == 0:
        return f"over the shoulder on the picture's {'right' if x > 0 else 'left'}"
    return f"over the {'near' if y < 0 else 'far'} shoulder"


def _back_view(heading: ViewId) -> str:
    """How much of the back a `heading` picture shows: in view, side-on or behind the body."""
    if "back" in _COVERAGE[heading]:
        return "in view"
    return "seen from the side" if FORWARD[heading][1] == 0 else "behind the body"


def placement_card(source: ViewId, target: ViewId, equipment: tuple[TurnaroundEquipment, ...]) -> list[str]:
    """What a correct `target` picture shows: its facing, then one sentence per item."""
    lines = [f"{FACING[target]}."]
    for row in equipment:
        side = body_side(source, row.source_picture_side)
        if row.attachment in _ATTACHMENT_NOUN and side is not None:
            location = _held(target, side, row.attachment)
        elif row.attachment == "back" and side is not None:
            location = f"on the back, {_over(target, side)}"
        elif row.attachment == "back":
            # No side claim for an unsided back item: only how much of the back is in view.
            location = f"on the back, {_back_view(target)}"
        else:
            location = "where the source shows it"
        lines.append(f"{row.item.capitalize()} {location}.")
    return lines


def synthesized_parts(source: ViewId, target: ViewId,
                      equipment: tuple[TurnaroundEquipment, ...] = ()) -> list[str]:
    """What the `target` picture shows that the source never did: labelled synthesized.

    An unsided back item hidden behind the body at the source but seen at the
    target is invented too; when the whole back is new, its line already covers
    it. A sided back item was seen in the source, over a shoulder, so it is not.
    """
    unseen = _COVERAGE[target] - _COVERAGE[source]
    parts = [text for part, text in _PARTS if part in unseen]
    if "back" not in unseen and _back_view(source) == "behind the body" and _back_view(target) != "behind the body":
        parts += [f"the {row.item} on the back" for row in equipment
                  if row.attachment == "back" and row.source_picture_side == "unsided"]
    return parts


def cards(source: ViewId, equipment: tuple[TurnaroundEquipment, ...]) -> dict[str, Any]:
    """Cards and synthesized parts for every heading but the source's own."""
    targets = [heading for heading in FORWARD if heading is not source]
    return {
        "cards_version": CARDS_VERSION,
        "placement_cards": {h.value: placement_card(source, h, equipment) for h in targets},
        "synthesized_parts": {h.value: synthesized_parts(source, h, equipment) for h in targets},
    }
