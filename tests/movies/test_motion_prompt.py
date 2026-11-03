"""Motion takes speak MiniMax H3's own image-to-video prompt format: the first frame is
referenced, the camera and what must stay unchanged are explicit, timed beats run in
order, and the look comes from the frame rather than a style paragraph."""

import pytest
from pydantic import ValidationError

from magic_video_editor.movies import motion
from magic_video_editor.movies.models import ShotInput
from magic_video_editor.movies.service import MOTION_SILENCE

from .test_service import setup

REFERENCE = (
    "For the target video, at 0.00 seconds into the target video, <Picture 1> "
    "(from [Shot 1]) is fully referenced."
)


def _shot(**fields):
    return ShotInput(shot_id="ramp", name="Ramp", duration_seconds=12, **{"prompt": "Crawlers drive down the ramp.", **fields})


def test_a_motion_prompt_uses_the_official_image_to_video_structure():
    prompt = motion.build_prompt(
        _shot(
            camera="The camera pushes in with small amplitude at slow speed.",
            still="the ramp, hull walls and lights",
            beats=[
                {"at_seconds": 6, "action": "the last crawler clears the ramp"},
                {"at_seconds": 1.5, "action": "the first crawler tips over the ramp's lip"},
            ],
            soundscape="Heavy treads on steel, wind outside.",
        ),
        continues=False,
    )
    head, description, soundscape, music = prompt.split("\n\n")
    assert head == REFERENCE
    assert description.startswith("integrated_multimodal_description: [Shot 1] ")
    assert "<Picture 1>" in description
    assert "preserving the ramp, hull walls and lights" in description
    assert "The camera pushes in with small amplitude at slow speed." in description
    assert "Crawlers drive down the ramp." in description
    assert description.index("At about 1.5 seconds, the first crawler") < description.index(
        "At about 6 seconds, the last crawler"
    )
    assert MOTION_SILENCE in description
    assert soundscape == "overall_soundscape: Heavy treads on steel, wind outside."
    assert music == "non_diegetic_music: N/A"


def test_a_bare_motion_shot_keeps_its_frame_and_leaves_camera_and_medium_to_the_shot():
    prompt = motion.build_prompt(_shot(prompt="The camera pans left as the gate opens."), continues=False)
    # Shots written before the camera field carry their camera move in the action;
    # an empty field must not contradict it, and the opening image sets the medium.
    assert "Static Shot" not in prompt
    assert "Live-action" not in prompt
    assert "in the same medium and look" in prompt
    assert "The camera pans left as the gate opens." in prompt
    assert "preserving its framing, lighting, structures, vehicles, people and terrain" in prompt
    assert "Solid objects keep their shape and size" in prompt
    assert "overall_soundscape: The natural ambience of the scene." in prompt


def test_beat_times_read_naturally():
    prompt = motion.build_prompt(_shot(beats=[{"at_seconds": 1, "action": "a crawler drives into frame"}]), continues=False)
    assert "At about 1 second, a crawler drives into frame." in prompt
    # Entering frame is allowed; popping into existence is not.
    assert "nothing pops into or out of existence" in prompt


def test_a_continuation_starts_from_the_preceding_shots_final_moment():
    prompt = motion.build_prompt(_shot(), continues=True)
    assert "[Shot 1] The final moment of the preceding shot, shown in <Picture 1>," in prompt


def test_a_beat_must_land_inside_the_shot():
    with pytest.raises(ValidationError, match="inside the shot"):
        _shot(beats=[{"at_seconds": 12, "action": "too late"}])


@pytest.mark.asyncio
async def test_a_generated_motion_take_submits_the_structured_prompt():
    service, pool, movie = await setup()
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "structured")
    take = movie.shots[0].takes[0]
    assert take.prompt == motion.build_prompt(movie.shots[0], continues=False)
    assert pool.calls[-1].payload["prompt"] == take.prompt
    assert take.prompt.startswith(REFERENCE)
    # The first frame carries the look; the style paragraph is not repeated to H3.
    assert "cinematic\nmotion" not in take.prompt


@pytest.mark.asyncio
async def test_a_build_fingerprint_ignores_unset_motion_fields():
    """Builds in flight across the deploy recompute plan_digest on load; shots that
    do not use the new fields must hash exactly as before they existed."""
    from magic_video_editor.movies.build import plan_digest
    from magic_video_editor.movies.service import digest

    _, _, movie = await setup()
    legacy_fields = {"shot_id", "name", "prompt", "opening_prompt", "duration_seconds",
                     "continuation_of", "dialogue", "soundscape"}
    legacy = digest({
        "style": movie.style,
        "compute": movie.compute_target,
        "characters": [c.model_dump() for c in movie.characters],
        "shots": [{**s.model_dump(include=legacy_fields), "opening": s.opening_asset_key,
                   "approved": s.opening_approved} for s in movie.shots],
        "soundtracks": [t.model_dump() for t in movie.soundtracks],
        "cut": [c.model_dump() for c in movie.cut],
    })
    assert plan_digest(movie) == legacy
    movie.shots[0].camera = "The camera pushes in with small amplitude at slow speed."
    assert plan_digest(movie) != legacy
