"""Portraits and the chained storyboard: one composed frame per shot, each drawn
from the cast's reference sheets and the previous frame."""

from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

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
            str(request.job_id),
            SimpleNamespace(status="queued", output_key=request.output_key),
        )

    async def get(self, job_id):
        return self.jobs.get(str(job_id))

    async def cancel(self, job_id):
        return True


def _png(color):
    data = BytesIO()
    Image.new("RGB", (1024, 1024), color).save(data, "PNG")
    return data.getvalue()


async def _land(service, pool, submission, color="blue"):
    await service.assets.write_once(submission.output_key, _png(color), "image/png")
    pool.jobs[str(submission.job_id)].status = "completed"


async def setup():
    pool = Pool()
    service = MovieService(MemoryMovieStore(), MemoryAssetStore(), pool)
    movie = await service.create("owner", "Film")
    movie = await service.characters(
        movie.movie_id,
        "owner",
        movie.revision,
        [
            {"character_id": "mara", "name": "Mara", "appearance": "short dark curls"},
            {"character_id": "ivo", "name": "Ivo", "appearance": "cropped hair, lean"},
        ],
    )
    movie = await service.story(
        movie.movie_id,
        "owner",
        movie.revision,
        brief="b",
        style="grainy 16 mm",
        shots=[
            {"shot_id": "a", "name": "A", "prompt": "static", "opening_prompt": "two at a window",
             "dialogue": [{"character_id": "mara", "text": "Hi."}]},
            {"shot_id": "b", "name": "B", "prompt": "static", "opening_prompt": "he turns",
             "continuation_of": "a", "dialogue": [{"character_id": "ivo", "text": "Hi."}]},
        ],
        batch_size=2,
    )
    return service, pool, movie


@pytest.mark.asyncio
async def test_a_portrait_is_composed_from_the_appearance_and_kept_until_it_changes():
    service, pool, movie = await setup()
    movie = await service.portrait(movie.movie_id, "owner", movie.revision, "mara", "p1")
    submission = pool.calls[-1]
    assert submission.capability_id == "image.flux2_klein.subject.v2"
    assert "Character reference sheet: short dark curls." in submission.payload["prompt"]
    assert "images" not in submission.payload
    await _land(service, pool, submission)
    movie = await service.reconcile(movie.movie_id, "owner")
    mara = movie.characters[0]
    assert mara.portrait_asset_key == movie.compositions["p1"]["output_key"]
    # Re-saving the cast unchanged keeps the portrait; a new look drops it.
    cast = [c.model_dump(include=set(type(c).model_fields) - {"voice_seconds", "portrait_asset_key", "portrait_appearance"}) for c in movie.characters]
    movie = await service.characters(movie.movie_id, "owner", movie.revision, cast)
    assert movie.characters[0].portrait_asset_key == mara.portrait_asset_key
    cast[0]["appearance"] = "grey hair"
    movie = await service.characters(movie.movie_id, "owner", movie.revision, cast)
    assert movie.characters[0].portrait_asset_key is None


@pytest.mark.asyncio
async def test_a_continuation_with_the_same_opening_shares_its_predecessors_frame():
    service, pool, movie = await setup()
    for cid, rid in (("mara", "p1"), ("ivo", "p2")):
        movie = await service.portrait(movie.movie_id, "owner", movie.revision, cid, rid)
        await _land(service, pool, pool.calls[-1])
        movie = await service.reconcile(movie.movie_id, "owner")
    shots = [
        {"shot_id": "a", "name": "A", "prompt": "static", "opening_prompt": "two at a window",
         "dialogue": [{"character_id": "mara", "text": "Hi."}]},
        {"shot_id": "b", "name": "B", "prompt": "static", "opening_prompt": "two at a window",
         "continuation_of": "a", "dialogue": [{"character_id": "ivo", "text": "Hi."}]},
        {"shot_id": "c", "name": "C", "prompt": "static", "opening_prompt": "two at a window, a map held up",
         "continuation_of": "b", "dialogue": [{"character_id": "ivo", "text": "Look."}]},
    ]
    movie = await service.story(movie.movie_id, "owner", movie.revision, brief="b", style="s", shots=shots, batch_size=2)
    portraits = {c.character_id: c.portrait_asset_key for c in movie.characters}
    movie = await service.storyboard(movie.movie_id, "owner", movie.revision, "sb")
    assert movie.compositions["sb:b"]["provider"] == "reuse"
    assert movie.compositions["sb:c"]["provider"] == "reuse"
    # The chain's one frame is composed from everyone who speaks in it.
    head = pool.calls[-1]
    assert head.payload["images"] == {"ref_1": portraits["mara"], "ref_2": portraits["ivo"]}
    assert "Exactly 2 people in frame" in head.payload["prompt"]
    assert "Mara (reference image 1: short dark curls); Ivo (reference image 2: cropped hair, lean)" in head.payload["prompt"]
    assert "no split screen" in head.payload["prompt"]
    jobs_before = {call.job_id for call in pool.calls}
    await _land(service, pool, head, "red")
    movie = await service.reconcile(movie.movie_id, "owner")
    frame_a = movie.compositions["sb:a"]["output_key"]
    assert [s.opening_asset_key for s in movie.shots] == [frame_a, frame_a, frame_a]
    assert {call.job_id for call in pool.calls} == jobs_before


@pytest.mark.asyncio
async def test_storyboard_frames_chain_from_portraits_and_the_previous_frame():
    service, pool, movie = await setup()
    with pytest.raises(MovieError, match="portrait for Mara"):
        await service.storyboard(movie.movie_id, "owner", movie.revision, "sb")
    for cid, rid in (("mara", "p1"), ("ivo", "p2")):
        movie = await service.portrait(movie.movie_id, "owner", movie.revision, cid, rid)
        await _land(service, pool, pool.calls[-1])
        movie = await service.reconcile(movie.movie_id, "owner")
    portraits = {c.character_id: c.portrait_asset_key for c in movie.characters}

    movie = await service.storyboard(movie.movie_id, "owner", movie.revision, "sb")
    # b continues a, so the chain has one frame drawn from both speakers.
    first = pool.calls[-1]
    assert first.job_id == movie.compositions["sb:a"]["job_id"]
    assert first.payload["images"] == {"ref_1": portraits["mara"], "ref_2": portraits["ivo"]}
    assert set(first.input_keys) == {portraits["mara"], portraits["ivo"]}
    assert "two at a window" in first.payload["prompt"]
    assert movie.compositions["sb:b"]["provider"] == "reuse"
    await _land(service, pool, first, "red")
    movie = await service.reconcile(movie.movie_id, "owner")
    frame_a = movie.compositions["sb:a"]["output_key"]
    assert movie.shots[0].opening_asset_key == frame_a
    assert movie.shots[1].opening_asset_key == frame_a
    frame = await service.assets.read_limited(movie.shots[1].opening_asset_key, 10_000_000)
    assert Image.open(BytesIO(frame)).size == (768, 432)
    # Re-posting the same request only reconciles: no new jobs, same revision.
    jobs = {call.job_id for call in pool.calls}
    again = await service.storyboard(movie.movie_id, "owner", movie.revision, "sb")
    assert {call.job_id for call in pool.calls} == jobs
    assert len(jobs) == 3 and again.revision == movie.revision
