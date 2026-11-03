from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from magic_video_editor.flights import prompt
from magic_video_editor.flights.service import LegInput, SceneFlightService
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.service import MovieError, MovieService
from magic_video_editor.movies.store import MemoryMovieStore


class Pool:
    def __init__(self):
        self.jobs = {}
        self.calls = []

    async def submit(self, request):
        self.calls.append(request)
        self.jobs.setdefault(
            request.job_id, SimpleNamespace(status="queued", output_key=request.output_key, preset="balanced")
        )

    async def get(self, job_id):
        return self.jobs.get(job_id)

    async def cancel(self, job_id):
        self.jobs[job_id].status = "cancelled"
        return True


class Media:
    """Pretends the clip's last frame is exactly the frozen destination."""

    def __init__(self, assets):
        self.assets = assets

    async def duration_for_key(self, key):
        return 5.1667

    async def final_frame(self, key):
        return key + ".last.png"

    async def assemble(self, sources, key, *, soundtracks=()):
        return key


def png(color, size=(1536, 864)):
    out = BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


async def setup(available=True):
    assets = MemoryAssetStore()
    pool = Pool()
    movies = MovieService(MemoryMovieStore(), assets, pool, media=Media(assets), scene_flights_available=available)
    service = SceneFlightService(movies)
    flight = await service.create("owner", "Harrowfen to Miretrace")
    origin = await service.add_image(flight["flight_id"], "owner", png("green"), "image/png")
    destination = await service.add_image(flight["flight_id"], "owner", png("grey"), "image/png")
    flight = await service.set_legs(
        flight["flight_id"], "owner", destination["revision"],
        [LegInput(leg_id="a", origin_image_key=origin["image_key"], destination_image_key=destination["image_key"],
                  origin_name="Harrowfen", destination_name="Miretrace")],
    )
    return service, pool, assets, flight, origin, destination


def test_default_leg_wording_is_gaias_scene_flight_leg_prompt_v1():
    assert prompt.default_leg_prompt("Harrowfen", "Miretrace", "A drowned village.", "") == (
        "Single continuous cinematic camera move, no cuts. Begin already drifting slowly "
        "forward, and end settling back into a slow steady forward drift. Never pull backward. "
        "Travel from Harrowfen to Miretrace. Departing: A drowned village. Empty and unoccupied. "
        "No people, no figures, no creatures. Cinematic high-fantasy environment, consistent art "
        "direction and lighting across the whole move. No text, no letters, no captions, no "
        "signage, no watermarks."
    )


def test_provider_prompt_pins_both_pictures_and_states_what_to_avoid():
    text = prompt.build_prompt("Fly low over the reeds", "birds")
    assert "<Picture 1> is fully referenced" in text
    assert "final frame of the target video is <Picture 2>" in text
    assert "Fly low over the reeds." in text
    assert "Avoid: birds." in text
    assert "Avoid" not in prompt.build_prompt("Fly low")


async def test_identical_images_share_one_immutable_key():
    service, _, _, flight, origin, _ = await setup()
    again = await service.add_image(flight["flight_id"], "owner", png("green"), "image/png")
    assert again["image_key"] == origin["image_key"]
    assert (again["width"], again["height"]) == (1536, 864)
    assert len((await service.get(flight["flight_id"], "owner"))["images"]) == 2


async def test_leg_submits_frame_pinned_fl2v_job_on_the_movie_canvas():
    service, pool, assets, flight, _origin, _destination = await setup()
    flight = await service.generate(flight["flight_id"], "owner", flight["revision"], "a", "r1", seed=7)
    request = pool.calls[0]
    assert request.capability_id == "video.minimax_h3.text_to_video.v1"
    assert request.requirements.min_vram_mb == 24_000
    payload = request.payload
    assert (payload["width"], payload["height"], payload["fps"], payload["steps"]) == (768, 448, 24, 20)
    assert payload["length"] == 124  # 5 s on H3's 17k+5 grid
    assert payload["seed"] == 7
    assert set(payload["images"]) == {"first_frame", "last_frame"}
    assert request.input_keys == (payload["images"]["first_frame"], payload["images"]["last_frame"])
    assert "Travel from Harrowfen to Miretrace" in payload["prompt"]
    for key in request.input_keys:
        with Image.open(BytesIO(await assets.read(key))) as frozen:
            assert frozen.size == (768, 448)
    take = flight["legs"][0]["takes"][0]
    assert take["status"] == "running"
    assert (take["seed"], take["steps"], take["capability_id"]) == (7, 20, "video.minimax_h3.text_to_video.v1")


async def test_completed_take_records_seam_diffs_and_is_selected_then_rerolled():
    service, pool, assets, flight, *_ = await setup()
    id = flight["flight_id"]
    flight = await service.generate(id, "owner", flight["revision"], "a", "r1")
    request = pool.calls[0]
    # A clip that starts on the origin and ends on the destination.
    await assets.write_once(request.output_key, b"mp4", "video/mp4")
    await assets.write_once(
        request.output_key + ".last.png", await assets.read(request.payload["images"]["last_frame"]), "image/png"
    )
    first_frame = await assets.read(request.payload["images"]["first_frame"])

    from magic_video_editor.flights import media

    original = media._first_frame
    media._first_frame = lambda content: first_frame
    try:
        pool.jobs[request.job_id].status = "completed"
        flight = await service.get(id, "owner")
    finally:
        media._first_frame = original
    leg = flight["legs"][0]
    take = leg["takes"][0]
    assert take["status"] == "succeeded"
    assert (take["first_frame_diff"], take["last_frame_diff"]) == (0.0, 0.0)
    assert take["executor_preset"] == "balanced"
    assert leg["selected_take_id"] == take["take_id"]
    assert take["video_key"] == request.output_key

    flight = await service.generate(id, "owner", flight["revision"], "a", "r2")
    assert len(pool.calls) == 2
    assert pool.calls[1].payload["seed"] != request.payload["seed"]
    assert flight["legs"][0]["selected_take_id"] == take["take_id"]


async def test_editing_a_leg_stales_its_takes_and_clears_selection():
    service, _pool, _, flight, origin, destination = await setup()
    id = flight["flight_id"]
    flight = await service.generate(id, "owner", flight["revision"], "a", "r1")
    flight = await service.set_legs(id, "owner", flight["revision"], [
        LegInput(leg_id="a", origin_image_key=origin["image_key"], destination_image_key=destination["image_key"],
                 prompt="Rise over the fen and descend into the mire")])
    take = flight["legs"][0]["takes"][0]
    assert take["stale"] is True
    assert flight["legs"][0]["prompt"] == "Rise over the fen and descend into the mire"


async def test_legs_only_use_images_granted_to_the_flight():
    service, _, _, flight, origin, _ = await setup()
    other = await service.create("owner", "Other")
    foreign = await service.add_image(other["flight_id"], "owner", png("red"), "image/png")
    with pytest.raises(MovieError) as error:
        await service.set_legs(flight["flight_id"], "owner", flight["revision"], [
            LegInput(leg_id="a", origin_image_key=origin["image_key"], destination_image_key=foreign["image_key"], prompt="go")])
    assert error.value.status == 403


async def test_flights_are_refused_without_a_last_frame_executor():
    service, pool, _, flight, *_ = await setup(available=False)
    with pytest.raises(MovieError) as error:
        await service.generate(flight["flight_id"], "owner", flight["revision"], "a", "r1")
    assert error.value.status == 503
    assert pool.calls == []


async def test_chain_requires_shared_boundaries_and_selected_takes():
    service, pool, assets, flight, origin, destination = await setup()
    id = flight["flight_id"]
    third = await service.add_image(id, "owner", png("blue"), "image/png")
    legs = [
        LegInput(leg_id="a", origin_image_key=origin["image_key"], destination_image_key=destination["image_key"], prompt="one"),
        LegInput(leg_id="b", origin_image_key=origin["image_key"], destination_image_key=third["image_key"], prompt="two"),
    ]
    flight = await service.set_legs(id, "owner", third["revision"], legs)
    with pytest.raises(MovieError, match="must start on the image"):
        await service.chain(id, "owner", flight["revision"], "c1")
    legs[1] = LegInput(leg_id="b", origin_image_key=destination["image_key"], destination_image_key=third["image_key"], prompt="two")
    flight = await service.set_legs(id, "owner", flight["revision"], legs)
    with pytest.raises(MovieError, match="select a successful take"):
        await service.chain(id, "owner", flight["revision"], "c1")
    for leg in ("a", "b"):
        flight = await service.generate(id, "owner", flight["revision"], leg, f"r-{leg}")
    for request in pool.calls:
        await assets.write_once(request.output_key, b"mp4", "video/mp4")
        pool.jobs[request.job_id].status = "completed"
    flight = await service.get(id, "owner")
    flight = await service.chain(id, "owner", flight["revision"], "c1")
    assert flight["chain"]["status"] == "succeeded"
    assert flight["chain"]["video_key"].startswith(f"outputs/movies/{id}/cuts/")


async def test_movie_story_edits_cannot_reach_a_flight():
    service, _, _, flight, *_ = await setup()
    with pytest.raises(MovieError, match="scene-flight legs"):
        await service.movies.story(
            flight["flight_id"], "owner", flight["revision"], brief="", style="", shots=[], batch_size=1
        )
