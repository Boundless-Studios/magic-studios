"""Prompts for scene-flight legs: one camera move pinned at both ends.

A leg's first frame is the origin image and its last frame the destination
image (H3 FL2V: ``first_frame`` and ``last_frame`` on MiniMaxH3ImageToVideo).
The node tokenizes the two images as ``<Picture 1>`` and ``<Picture 2>``, so
the wrapper names both and states where each one lands; the leg's own wording
then only has to describe the move.

``default_leg_prompt`` reproduces Gaia's ``scene_flight_leg`` user prompt
(version 1) clause for clause, so a caller that sends only place names gets
the same art direction, emptiness rule, no-text guard and motion handoff that
Gaia's Runware legs use. Callers that author their own wording send ``prompt``.
"""

from __future__ import annotations

MAX_PROMPT_CHARS = 2000

REFERENCE = (
    "For the target video, at 0.00 seconds into the target video, <Picture 1> "
    "is fully referenced, and the final frame of the target video is <Picture 2>, "
    "fully referenced."
)
SOUNDSCAPE = "Quiet natural ambience of the places; no voices and no music."


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    return text if not text or text.endswith((".", "!", "?")) else text + "."


def default_leg_prompt(
    origin_name: str,
    destination_name: str,
    origin_description: str = "",
    destination_description: str = "",
) -> str:
    """Gaia's scene-flight leg wording, single-spaced as Gaia sends it."""
    clauses = [
        "Single continuous cinematic camera move, no cuts.",
        ("Begin already drifting slowly forward, and end settling back into a slow "
        "steady forward drift. Never pull backward."),
        f"Travel from {origin_name.strip()} to {destination_name.strip()}.",
        f"Departing: {origin_description.strip()}" if origin_description.strip() else "",
        f"Arriving: {destination_description.strip()}"
        if destination_description.strip()
        else "",
        "Empty and unoccupied. No people, no figures, no creatures.",
        ("Cinematic high-fantasy environment, consistent art direction and lighting "
        "across the whole move."),
        "No text, no letters, no captions, no signage, no watermarks.",
    ]
    return " ".join(" ".join(c for c in clauses if c).split())


def build_prompt(direction: str, negative_prompt: str = "") -> str:
    """The provider prompt for one leg. H3's graph has no negative conditioning
    (BasicGuider, no CFG), so wording to avoid is stated in the positive prompt."""
    parts = [
        ("[Shot 1] The camera departs from the scene shown in <Picture 1> and "
        "arrives exactly on the scene shown in <Picture 2>, as one unbroken take "
        "in the same medium and look as both pictures."),
        _sentence(direction),
    ]
    if negative_prompt.strip():
        parts.append(_sentence(f"Avoid: {negative_prompt.strip()}"))
    parts.append(
        "Solid structures and terrain keep their shape and size: nothing bends, "
        "melts, morphs or flickers. The final frame matches <Picture 2>."
    )
    return "\n\n".join(
        [
            REFERENCE,
            "integrated_multimodal_description: " + " ".join(parts),
            "overall_soundscape: " + SOUNDSCAPE,
            "non_diegetic_music: N/A",
        ]
    )
