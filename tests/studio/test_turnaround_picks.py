"""Frame suggestions against real turntables picked by eye."""

import json
from pathlib import Path

import pytest

from magic_video_editor.studio.models import ViewId
from magic_video_editor.studio.turnaround_picks import (
    SUPPORTED_SOURCES, TURN, PickUnavailable, step, suggest, torso_width,
)
from magic_video_editor.studio.turnarounds import (
    QUALIFIED_SOURCES, TurnaroundUnavailable, require_qualified_source,
)

FIXTURES = Path(__file__).parent / "turnaround_fixtures"
MIRROR = {ViewId.E: ViewId.W, ViewId.W: ViewId.E, ViewId.SE: ViewId.SW, ViewId.SW: ViewId.SE,
          ViewId.NE: ViewId.NW, ViewId.NW: ViewId.NE, ViewId.N: ViewId.N, ViewId.S: ViewId.S}


def fixture(name):
    document = json.loads((FIXTURES / f"{name}.json").read_text())
    picks = {ViewId(h): frame - 1 for h, frame in document["picks_1based"].items()}
    return document["torso_width"], ViewId(document["source_heading"]), picks


def mirrored_reverse(name):
    """An SW-source loop from an SE one: mirroring swaps E/W and reverses the turn,
    and playing it backwards restores the prompted direction. Torso width is
    mirror-invariant, so only the time axis and the labels change."""
    widths, source, picks = fixture(name)
    last = len(widths) - 1
    flipped = {MIRROR[h]: (last - frame) % last if frame else 0 for h, frame in picks.items()}
    return list(reversed(widths)), MIRROR[source], flipped


def errors(widths, source, picks):
    suggested = suggest(widths, source)
    return {heading: suggested[heading] - picks[heading] for heading in TURN}


# Pinned so a change to the picker is deliberate. Errors are suggestion minus eye
# pick, in frames of a 107-frame loop (about 3.4 degrees each). The orc's E and W
# picks favour the strictest profile, five frames past the narrowest torso.
EXPECTED = {
    "orc_se": {ViewId.S: 1, ViewId.SE: 0, ViewId.E: -5, ViewId.NE: 1, ViewId.N: 1,
               ViewId.NW: -1, ViewId.W: -5, ViewId.SW: -1},
    "ranger_s": {ViewId.S: 0, ViewId.SE: 2, ViewId.E: 3, ViewId.NE: 3, ViewId.N: 7,
                 ViewId.NW: 6, ViewId.W: 2, ViewId.SW: 5},
    # The 2026-10-23 three-quarter qualification: every suggestion was on its heading.
    "ranger_se": {heading: 0 for heading in TURN},
    "orc_sw_mirror": {heading: 0 for heading in TURN},
    "ranger_sw_mirror": {heading: 0 for heading in TURN},
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_suggestions_match_eye_picks_on_real_turntables(name):
    assert errors(*fixture(name)) == EXPECTED[name]
    # About 24 degrees at worst; the ranger's cloak blurs its back view.
    assert max(map(abs, EXPECTED[name].values())) <= 7


def test_the_mirrored_orc_places_an_sw_source_the_same_way():
    widths, source, picks = mirrored_reverse("orc_se")
    assert source is ViewId.SW
    found = errors(widths, source, picks)
    assert found[ViewId.SW] == 0
    assert max(map(abs, found.values())) <= 5


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_suggestions_run_in_turn_order_from_the_source(name):
    widths, source, _ = fixture(name)
    suggested = suggest(widths, source)
    ordered = sorted(TURN, key=lambda heading: step(heading, source))
    assert ordered[0] is source and suggested[source] == 0
    frames = [suggested[heading] for heading in ordered]
    assert frames == sorted(frames) and len(set(frames)) == len(frames)


def test_the_seam_is_never_searched_for_an_anchor():
    widths, source, _ = fixture("orc_se")
    # Narrow frames near the loop's join (a keying glitch) must not become E or W.
    widths = [*[0.01] * 6, *widths[6:-6], *[0.01] * 6]
    assert suggest(widths, source)[ViewId.E] == 27


@pytest.mark.parametrize("source", sorted(set(TURN) - SUPPORTED_SOURCES))
def test_seam_and_untested_sources_are_refused(source):
    widths, _, _ = fixture("orc_se")
    with pytest.raises(PickUnavailable):
        suggest(widths, source)


def test_a_signal_without_two_profiles_is_refused():
    with pytest.raises(PickUnavailable):
        suggest([0.3] * 20, ViewId.S)


def test_torso_width_measures_the_band_not_the_feet_or_a_low_weapon():
    figure = [[False] * 20 for _ in range(20)]
    for row in range(20):
        for column in range(8, 12):
            figure[row][column] = True
    for column in range(0, 20):  # a wide weapon held at knee height
        figure[15][column] = True
    assert torso_width(figure) == 4 / 20
    assert torso_width([[False] * 4] * 4) == 0.0


def test_front_and_front_three_quarter_sources_start_and_the_others_say_why_not():
    assert QUALIFIED_SOURCES == {ViewId.S, ViewId.SE, ViewId.SW} <= SUPPORTED_SOURCES
    for heading in QUALIFIED_SOURCES:
        require_qualified_source(heading)
    reasons = {}
    for heading in [*set(TURN) - QUALIFIED_SOURCES, None]:
        with pytest.raises(TurnaroundUnavailable) as refused:
            require_qualified_source(heading)
        assert "front-facing (S) or front three-quarter (SE, SW)" in str(refused.value)
        reasons[heading] = str(refused.value)
    assert "seam" in reasons[ViewId.E] and "seam" in reasons[ViewId.N]
    assert "untested" in reasons[ViewId.NE]
    assert "no compass heading" in reasons[None]


def _set(frames, **overrides):
    from magic_video_editor.studio.models import TurnaroundPick
    return tuple(TurnaroundPick(heading=heading, frame_index=frame, **overrides.get(heading.value, {}))
                 for heading, frame in frames.items())


def test_a_pick_set_on_the_suggestions_passes_without_findings():
    from magic_video_editor.studio.turnaround_picks import check_picks
    widths, source, _ = fixture("ranger_s")
    suggested = suggest(widths, source)
    picks = {h: f for h, f in suggested.items() if h is not source}
    assert check_picks(_set(picks), suggested, source, len(widths)) == []


def test_the_source_is_an_anchor_and_every_other_heading_is_required():
    from magic_video_editor.studio.turnaround_picks import PickSetRejected, check_picks
    widths, source, _ = fixture("ranger_s")
    suggested = suggest(widths, source)
    with pytest.raises(PickSetRejected, match="is the source"):
        check_picks(_set(suggested), suggested, source, len(widths))
    partial = {h: f for h, f in suggested.items() if h not in (source, ViewId.N)}
    with pytest.raises(PickSetRejected, match="seven other headings"):
        check_picks(_set(partial), suggested, source, len(widths))


def test_out_of_region_and_out_of_order_picks_need_their_own_confirmation():
    from magic_video_editor.studio.turnaround_picks import PickSetRejected, check_picks
    widths, source, _ = fixture("ranger_s")
    suggested = suggest(widths, source)
    picks = {h: f for h, f in suggested.items() if h is not source}
    # An NE pick taken from the NW side: outside its region and out of turn order.
    picks[ViewId.NE] = suggested[ViewId.NW]
    with pytest.raises(PickSetRejected) as refused:
        check_picks(_set(picks), suggested, source, len(widths))
    kinds = {(p["heading"], p["kind"]) for p in refused.value.problems}
    assert ("NE", "out_of_region") in kinds and ("NE", "out_of_order") in kinds
    # Confirming one rule is not enough; each needs its own confirmation.
    with pytest.raises(PickSetRejected):
        check_picks(_set(picks, NE={"confirm_out_of_region": True}), suggested, source, len(widths))
    findings = check_picks(_set(picks, NE={"confirm_out_of_region": True, "confirm_out_of_order": True},
                                N={"confirm_out_of_order": True}, NW={"confirm_out_of_order": True}),
                           suggested, source, len(widths))
    region = next(f for f in findings if f["kind"] == "out_of_region")
    assert region["heading"] == "NE" and region["distance"] > 0


def test_frames_wrap_on_the_unwrapped_loop_so_sw_sits_between_w_and_the_end():
    widths, source, _ = fixture("ranger_s")
    suggested = suggest(widths, source)
    assert suggested[ViewId.W] < suggested[ViewId.SW] < len(widths)


@pytest.mark.parametrize("widths", [[0.3] * 107, [0.3 + 0.001 * (i % 3) for i in range(107)]])
def test_a_take_that_barely_turns_gets_no_suggestions(widths):
    with pytest.raises(PickUnavailable, match="does not turn enough"):
        suggest(widths, ViewId.S)


def test_the_last_frame_is_the_source_again_so_it_is_never_a_free_sw_pick():
    from magic_video_editor.studio.turnaround_picks import PickSetRejected, check_picks
    widths, source, _ = fixture("ranger_s")
    suggested = suggest(widths, source)
    picks = {h: f for h, f in suggested.items() if h is not source}
    picks[ViewId.SW] = len(widths) - 1  # the closed loop's terminal frame shows S, not SW
    with pytest.raises(PickSetRejected) as refused:
        check_picks(_set(picks), suggested, source, len(widths))
    assert ("SW", "out_of_order") in {(p["heading"], p["kind"]) for p in refused.value.problems}


def test_a_take_that_narrows_only_once_gets_no_suggestions():
    # One five-frame trough passes the global contrast check, but there is no second profile.
    widths = [1.0] * 107
    widths[25:30] = [0.5] * 5
    with pytest.raises(PickUnavailable, match="narrows only once"):
        suggest(widths, ViewId.S)


def test_an_out_of_order_override_records_how_far_the_pick_must_move():
    from magic_video_editor.studio.turnaround_picks import PickSetRejected, check_picks
    widths, source, _ = fixture("ranger_s")
    suggested = suggest(widths, source)
    picks = {h: f for h, f in suggested.items() if h is not source}
    picks[ViewId.N] = suggested[ViewId.NE] - 3  # four frames short of following NE
    with pytest.raises(PickSetRejected) as refused:
        check_picks(_set(picks, N={"confirm_out_of_region": True}), suggested, source, len(widths))
    [order] = [p for p in refused.value.problems if p["kind"] == "out_of_order" and p["heading"] == "N"]
    assert order["distance"] == 4
    # NE, now not before N, is out of order too: by the same three frames plus one.
    confirmed = {"confirm_out_of_region": True, "confirm_out_of_order": True}
    findings = check_picks(_set(picks, N=confirmed, NE={"confirm_out_of_order": True}), suggested, source, len(widths))
    assert {"heading": "N", "kind": "out_of_order", "distance": 4} in findings
    assert {"heading": "NE", "kind": "out_of_order", "distance": 4} in findings


def test_neighbours_with_no_frame_between_them_give_no_distance():
    from magic_video_editor.studio.turnaround_picks import check_picks
    widths, source, _ = fixture("ranger_s")
    suggested = suggest(widths, source)
    picks = {h: f for h, f in suggested.items() if h is not source}
    # NE, N and NW all on one frame: nothing lies strictly between N's neighbours.
    picks[ViewId.NE] = picks[ViewId.NW] = picks[ViewId.N]
    every = {"confirm_out_of_region": True, "confirm_out_of_order": True}
    findings = check_picks(_set(picks, NE=every, N=every, NW=every), suggested, source, len(widths))
    [north] = [f for f in findings if f["heading"] == "N" and f["kind"] == "out_of_order"]
    assert north["distance"] is None
