"""Suggest one turntable frame per heading, relative to the source's heading.

The turnaround prompt fixes the order of the turn: from the source it turns
toward screen-right first, then away, then left, then back, so every heading
has a known place in the loop. Turns are not uniform (the SE orc takes a third
of the loop to reach E), so fixed windows are not enough. Instead the torso
width finds three anchors and the rest are spaced evenly between them:

- the two profiles, E and W, are the two narrowest torsos at least a quarter of
  the loop apart, in turn order;
- the back view, N, is the widest torso between them;
- every other heading is interpolated evenly between its neighbouring anchors,
  with the source at the first frame and again at the last (the loop closes).

Frames within a sixteenth of the seam are never searched. Suggestions are
proposals; the person owns every pick.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .models import ViewId

# The order the prompt visits headings, starting from S.
TURN = (ViewId.S, ViewId.SE, ViewId.E, ViewId.NE, ViewId.N, ViewId.NW, ViewId.W, ViewId.SW)
# Sources where E, N and W all fall strictly inside the loop. A profile or back
# source sits on the seam itself, so one anchor would be missing.
SUPPORTED_SOURCES = frozenset({ViewId.S, ViewId.SE, ViewId.SW})
_SMOOTHING = 5
# The torso must narrow by at least this share of its widest to count as turning;
# the real turntables measured 32% (ranger) and 52% (orc).
MIN_CONTRAST = 0.15


class PickUnavailable(ValueError):
    """The signal cannot place the anchors for this source."""


def torso_width(alpha) -> float:
    """Median opaque width of the 25-55% height band, over the figure's height.

    `alpha` is a boolean mask (rows of columns). The band ignores the feet and a
    low-held weapon. An empty mask is 0.
    """
    mask = np.asarray(alpha, dtype=bool)
    rows = np.flatnonzero(mask.any(axis=1))
    if not len(rows):
        return 0.0
    top, height = int(rows[0]), int(rows[-1] - rows[0] + 1)
    band = mask[top + int(0.25 * height): top + int(0.55 * height)]
    return float(np.median(band.sum(axis=1))) / height if len(band) else 0.0


def _smooth(values: Sequence[float]) -> list[float]:
    # Edge-padded moving average, so the ends average real frames.
    half = _SMOOTHING // 2
    padded = [values[0]] * half + list(values) + [values[-1]] * half
    return [sum(padded[i:i + _SMOOTHING]) / _SMOOTHING for i in range(len(values))]


def step(heading: ViewId, source: ViewId) -> int:
    """How many eighths of the turn separate `heading` from the source."""
    return (TURN.index(heading) - TURN.index(source)) % len(TURN)


def suggest(widths: Sequence[float], source: ViewId) -> dict[ViewId, int]:
    """0-based frame suggestions for all eight headings of a closed-loop turntable."""
    if source not in SUPPORTED_SOURCES:
        raise PickUnavailable(f"{source} sources put a picked heading on the seam")
    count = len(widths)
    if count < 32:
        raise PickUnavailable("the turntable is too short to place eight headings")
    smooth = _smooth(widths)
    seam = max(1, round(count / 16))
    inside = range(seam, count - seam)
    widest = max(smooth[i] for i in inside)
    if widest <= 0 or (widest - min(smooth[i] for i in inside)) / widest < MIN_CONTRAST:
        # A static or constant signal has no profiles or back to find: index
        # tie-breaks would pass for directions.
        raise PickUnavailable("the take does not turn enough to tell its directions apart")
    first = min(inside, key=lambda i: (smooth[i], i))
    apart = [i for i in inside if abs(i - first) >= count / 4]
    if not apart:
        raise PickUnavailable("no second profile a quarter of the loop away")
    second = min(apart, key=lambda i: (smooth[i], i))
    if (widest - smooth[second]) / widest < MIN_CONTRAST:
        # The body narrowed only once (an incomplete turn, or a profile hidden by
        # equipment): any frame would do for the second profile, so none is right.
        raise PickUnavailable("the take narrows only once; there is no second profile")
    east, west = sorted((first, second))
    between = range(east + 1, west)
    if not between:
        raise PickUnavailable("the profiles leave no room for the back view")
    north = max(between, key=lambda i: (smooth[i], -i))
    anchors = sorted({
        (0, 0),
        (step(ViewId.E, source), east),
        (step(ViewId.N, source), north),
        (step(ViewId.W, source), west),
        (len(TURN), count - 1),
    })
    picks: dict[ViewId, int] = {}
    for heading in TURN:
        position = step(heading, source)
        exact = next((frame for at, frame in anchors if at == position), None)
        if exact is not None:
            picks[heading] = exact
            continue
        (low, low_frame), (high, high_frame) = next(
            (anchors[i], anchors[i + 1]) for i in range(len(anchors) - 1)
            if anchors[i][0] < position < anchors[i + 1][0])
        picks[heading] = round(low_frame + (high_frame - low_frame) * (position - low) / (high - low))
    ordered = [picks[h] for h in sorted(TURN, key=lambda h: step(h, source))]
    if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
        raise PickUnavailable("the measured directions do not follow the turn order")
    return picks


class PickSetRejected(ValueError):
    """A pick set broke a rule without the person's per-pick confirmation."""

    def __init__(self, problems: list[dict]):
        super().__init__("; ".join(problem["message"] for problem in problems))
        self.problems = problems


def check_picks(picks, suggested: dict[ViewId, int] | None, source: ViewId, frame_count: int) -> list[dict]:
    """Validate a whole pick set; return the overrides it used.

    The source is an anchor at frame 0 and again at `frame_count` (the loop
    closes), never a pick, so exactly the other seven headings are required.
    Positions are on that unwrapped loop. Each pick must lie in its heading's
    region (between its neighbours' suggestions, widened by a quarter of each
    gap) and the picks must run in turn order. Breaking either rule needs that
    pick's own confirmation; the finding records how far outside it was.

    The loop's last frame is the source again, so the final heading must come
    before frame `frame_count - 1`. Without suggestions (`suggested` None) there
    is no region to check against: every pick needs its confirmation, and turn
    order is still enforced.
    """
    by_heading = {pick.heading: pick for pick in picks}
    expected = [heading for heading in sorted(TURN, key=lambda h: step(h, source)) if heading is not source]
    if source in by_heading:
        raise PickSetRejected([{"heading": source.value, "kind": "source",
                                "message": f"{source.value} is the source; it is not picked"}])
    if set(by_heading) != set(expected) or len(picks) != len(expected):
        raise PickSetRejected([{"heading": None, "kind": "incomplete",
                                "message": "pick exactly one frame for each of the seven other headings"}])
    problems, findings = [], []
    end = frame_count - 1  # the source again, where the closed loop meets its start
    anchors = {0: 0, len(TURN): end}
    for heading in expected:
        pick = by_heading[heading]
        if not 0 <= pick.frame_index < frame_count:
            problems.append({"heading": heading.value, "kind": "range",
                             "message": f"{heading.value}: frame {pick.frame_index} is outside the take"})
            continue
        position = step(heading, source)
        if suggested is None:
            if pick.confirm_out_of_region:
                findings.append({"heading": heading.value, "kind": "unsuggested"})
            else:
                problems.append({"heading": heading.value, "kind": "out_of_region",
                                 "message": f"{heading.value}: Studio has no suggestion to check frame "
                                            f"{pick.frame_index} against; confirm it is the facing you mean"})
            low = high = None
        else:
            here = suggested[heading]
            before = anchors[0] if position == 1 else suggested[expected[position - 2]]
            after = anchors[len(TURN)] if position == len(expected) else suggested[expected[position]]
            low, high = before - (here - before) / 4, after + (after - here) / 4
        if low is not None and not low <= pick.frame_index <= high:
            distance = round(low - pick.frame_index if pick.frame_index < low else pick.frame_index - high)
            if pick.confirm_out_of_region:
                findings.append({"heading": heading.value, "kind": "out_of_region", "distance": distance})
            else:
                problems.append({"heading": heading.value, "kind": "out_of_region", "distance": distance,
                                 "message": f"{heading.value}: frame {pick.frame_index} is {distance} frames outside "
                                            "its region; confirm it if that is the facing you mean"})
        previous = 0 if position == 1 else by_heading[expected[position - 2]].frame_index
        following = end if position == len(expected) else by_heading[expected[position]].frame_index
        if not previous < pick.frame_index < following:
            # How many frames the pick must move to fall strictly between its neighbours;
            # None when the neighbours themselves leave no frame between them.
            distance = (None if following - previous < 2
                        else previous - pick.frame_index + 1 if pick.frame_index <= previous
                        else pick.frame_index - following + 1)
            if pick.confirm_out_of_order:
                findings.append({"heading": heading.value, "kind": "out_of_order", "distance": distance})
            else:
                problems.append({"heading": heading.value, "kind": "out_of_order", "distance": distance,
                                 "message": f"{heading.value}: frame {pick.frame_index} is out of turn order; "
                                            "confirm it if that is the facing you mean"})
    if problems:
        raise PickSetRejected(problems)
    return findings
