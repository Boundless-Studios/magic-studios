"""Prompts for motion takes in MiniMax H3's own image-to-video (I2VA) format.

H3's prompt guide (MiniMaxAI/MiniMax-H3 docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md):
an I2VA prompt opens by referencing the first frame, then gives three fields:
integrated_multimodal_description, overall_soundscape and non_diegetic_music. The
first frame carries the look, so the words go to motion: anchor what is visible and
must stay as it is, at most one camera move written as type + amplitude + speed
("The camera holds a Static Shot" when it must not move), the action
and its timed beats. Loose prose let the model invent motion (warping screens,
morphing walls); this states what moves and what does not.
"""

from __future__ import annotations

from magic_video_editor.movies.models import ShotInput

REFERENCE = (
    "For the target video, at 0.00 seconds into the target video, <Picture 1> "
    "(from [Shot 1]) is fully referenced."
)
PRESERVED = "its framing, lighting, structures, vehicles, people and terrain"
# "Nothing appears" would forbid things entering frame (a vehicle driving in from
# below); what must not happen is things popping into or out of existence.
PHYSICS = (
    "Solid objects keep their shape and size: nothing bends, melts, morphs or "
    "flickers, nothing pops into or out of existence, and everything that moves "
    "has weight and momentum."
)
SILENCE = (
    "Nobody speaks, hums, sings or makes any vocal sound; no narration. The only "
    "sound is the natural ambience of the scene."
)
AMBIENCE = "The natural ambience of the scene."


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _seconds(value: float) -> str:
    return f"{value:g} second" + ("" if value == 1 else "s")


def build_prompt(shot: ShotInput, *, continues: bool) -> str:
    source = (
        "the final moment of the preceding shot, shown in <Picture 1>,"
        if continues
        else "the scene shown in <Picture 1>"
    )
    # No medium is named (the opening image may be drawn, stop-motion, photoreal...)
    # and no camera is assumed: shots written before the camera field carry their
    # move in the action, so an empty field adds nothing that could contradict it.
    parts = [
        f"[Shot 1] {source[0].upper()}{source[1:]} continues as one unbroken camera "
        f"take in the same medium and look, preserving {shot.still.strip() or PRESERVED}.",
        *([_sentence(shot.camera)] if shot.camera.strip() else []),
        _sentence(shot.prompt),
        *(
            _sentence(f"At about {_seconds(b.at_seconds)}, {b.action}")
            for b in sorted(shot.beats, key=lambda b: b.at_seconds)
        ),
        PHYSICS,
        SILENCE,
    ]
    return "\n\n".join(
        [
            REFERENCE,
            "integrated_multimodal_description: " + " ".join(parts),
            "overall_soundscape: " + (_sentence(shot.soundscape) if shot.soundscape.strip() else AMBIENCE),
            "non_diegetic_music: N/A",
        ]
    )
