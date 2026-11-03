from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.routes import create_movie_router
from magic_video_editor.movies.service import MovieService
from magic_video_editor.movies.store import MemoryMovieStore
from magic_video_editor.studio.routes import studio_actor_id


def test_movie_routes_owner_auth_revision_and_capabilities():
    app = FastAPI()
    service = MovieService(MemoryMovieStore(), MemoryAssetStore())
    app.include_router(create_movie_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    client = TestClient(app)
    response = client.post("/studio/movies", json={"name": "Film"})
    assert response.status_code == 200
    movie = response.json()
    id = movie["movie_id"]
    assert client.get("/studio/movies").json()[0]["movie_id"] == id
    assert (
        client.get("/studio/movies/capabilities").json()["generation_available"]
        is False
    )
    assert (
        client.post(
            f"/studio/movies/{id}/generate",
            json={"expected_revision": 1, "shot_ids": ["a"], "request_id": "r"},
        ).status_code
        == 503
    )
    assert client.get(f"/studio/movies/{id}/history").json()[0]["revision"] == 1
    assert client.get(f"/studio/movies/{id}/selected-cut").json() == []
    app.dependency_overrides[studio_actor_id] = lambda: "other"
    assert client.get(f"/studio/movies/{id}").status_code == 404
    assert client.get(f"/studio/movies/{id}/selected-cut").status_code == 404
    assert client.get("/studio/movies").json() == []


def test_movie_shot_duration_matches_exact_provider_frame_budget():
    import pytest
    from pydantic import ValidationError

    from magic_video_editor.movies.models import ShotInput

    with pytest.raises(ValidationError):
        ShotInput(shot_id="s", name="s", prompt="move", duration_seconds=1)
    with pytest.raises(ValidationError):
        ShotInput(shot_id="s", name="s", prompt="move", duration_seconds=1.6)
    assert (
        ShotInput(
            shot_id="s", name="s", prompt="move", duration_seconds=1.5
        ).duration_seconds
        == 1.5
    )


def test_a_shot_runs_up_to_the_native_h3_limit_and_no_further():
    """362 frames at 24 fps is H3's trained ceiling; 15 s is the longest
    half-second request whose grid-aligned frame count still fits it."""
    import pytest
    from pydantic import ValidationError

    from magic_video_editor.movies import dialogue
    from magic_video_editor.movies.models import ShotInput

    longest = ShotInput(shot_id="s", name="s", prompt="move", duration_seconds=15)
    assert longest.duration_seconds == 15
    assert dialogue.motion_frames(15) == dialogue.MAX_FRAMES == 362
    with pytest.raises(ValidationError):
        ShotInput(shot_id="s", name="s", prompt="move", duration_seconds=15.5)
    assert dialogue.motion_frames(15.5) > dialogue.MAX_FRAMES

    app = FastAPI()
    app.include_router(create_movie_router(MovieService(MemoryMovieStore(), MemoryAssetStore())))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    capabilities = TestClient(app).get("/studio/movies/capabilities").json()
    assert capabilities["max_duration_seconds"] == 15
    assert capabilities["duration_step_seconds"] == 0.5


def test_a_story_accepts_a_fifteen_second_shot_and_refuses_a_longer_one():
    app = FastAPI()
    app.include_router(create_movie_router(MovieService(MemoryMovieStore(), MemoryAssetStore())))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    client = TestClient(app)
    movie = client.post("/studio/movies", json={"name": "Film"}).json()

    def story(revision: int, seconds: float):
        return client.put(
            f"/studio/movies/{movie['movie_id']}/story",
            json={
                "expected_revision": revision,
                "brief": "",
                "style": "",
                "shots": [{"shot_id": "a", "name": "A", "prompt": "move", "duration_seconds": seconds}],
            },
        )

    refused = story(movie["revision"], 15.5)
    accepted = story(movie["revision"], 15)

    assert refused.status_code == 422
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["shots"][0]["duration_seconds"] == 15


def test_release_download_uses_frozen_asset_and_is_owner_scoped():
    import asyncio
    from uuid import uuid4

    from magic_video_editor.movies.models import Release

    service = MovieService(MemoryMovieStore(), MemoryAssetStore())
    release_id = str(uuid4())

    async def setup_release():
        movie = await service.create("owner", "Film")
        key = f"outputs/movies/{movie.movie_id}/cuts/frozen.mp4"
        await service.assets.write_once(key, b"movie", "video/mp4")
        movie.releases = [
            Release(
                release_id=release_id,
                name="Film",
                asset_key=key,
                cut_digest="digest",
                source_revision=1,
            )
        ]
        return await service.save(movie, movie.revision)

    movie = asyncio.run(setup_release())
    app = FastAPI()
    app.include_router(create_movie_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    client = TestClient(app)
    route = f"/studio/movies/{movie.movie_id}/releases/{release_id}/download"
    response = client.get(route, follow_redirects=False)
    # Streamed to the owner, never redirected to a bearer storage URL.
    assert response.status_code == 200
    assert response.content == b"movie"
    assert response.headers["cache-control"].startswith("private")
    assert response.headers["content-disposition"].startswith(
        f'attachment; filename="movie-{release_id}.mp4"'
    )
    assert client.get(route.replace(release_id, str(uuid4()))).status_code == 404
    app.dependency_overrides[studio_actor_id] = lambda: "other"
    assert client.get(route).status_code == 404


def test_attach_voice_route_validates_and_freezes_granted_clip():
    import asyncio

    class AudioMedia:
        async def audio_duration(self, content):
            return 4.0

    service = MovieService(MemoryMovieStore(), MemoryAssetStore())
    service.media = AudioMedia()
    app = FastAPI()
    app.include_router(create_movie_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    client = TestClient(app)
    movie = client.post("/studio/movies", json={"name": "Film"}).json()
    base = f"/studio/movies/{movie['movie_id']}"
    movie = client.put(f"{base}/characters", json={
        "expected_revision": movie["revision"],
        "characters": [{"character_id": "man", "name": "Man"}],
    }).json()
    grant = client.post(f"{base}/uploads", json={"content_type": "audio/wav"}).json()
    asyncio.run(service.assets.write_once(grant["asset_key"], b"RIFF0000WAVE", "audio/wav"))
    body = {"expected_revision": grant["revision"], "asset_key": grant["asset_key"]}
    response = client.put(f"{base}/characters/man/voice", json=body)
    assert response.status_code == 200
    assert response.json()["characters"][0]["voice_seconds"] == 4.0
    assert client.put(f"{base}/characters/man/voice", json=body).status_code == 409
    app.dependency_overrides[studio_actor_id] = lambda: "other"
    assert client.put(f"{base}/characters/man/voice", json=body).status_code == 404


def test_movie_revision_conflict_reports_current_revision():
    app = FastAPI()
    service = MovieService(MemoryMovieStore(), MemoryAssetStore())
    app.include_router(create_movie_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    client = TestClient(app)
    movie = client.post("/studio/movies", json={"name": "Film"}).json()
    base = f"/studio/movies/{movie['movie_id']}"
    stale = {"expected_revision": movie["revision"] + 5,
             "characters": [{"character_id": "man", "name": "Man"}]}
    response = client.put(f"{base}/characters", json=stale)
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": f"Revision conflict; current revision is {movie['revision']}",
        "current_revision": movie["revision"],
    }


def test_release_download_reports_a_storage_outage_as_503():
    import asyncio
    from uuid import uuid4

    from magic_video_editor.contracts import AssetStorageUnavailable
    from magic_video_editor.movies.models import Release

    service = MovieService(MemoryMovieStore(), MemoryAssetStore())
    release_id = str(uuid4())

    async def setup_release():
        movie = await service.create("owner", "Film")
        key = f"outputs/movies/{movie.movie_id}/cuts/frozen.mp4"
        await service.assets.write_once(key, b"movie", "video/mp4")
        movie.releases = [
            Release(
                release_id=release_id, name="Film", asset_key=key,
                cut_digest="digest", source_revision=1,
            )
        ]
        return await service.save(movie, movie.revision)

    movie = asyncio.run(setup_release())

    async def unreachable(_key):
        raise AssetStorageUnavailable("bucket unreachable")

    service.assets.describe = unreachable
    app = FastAPI()
    app.include_router(create_movie_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    response = TestClient(app).get(
        f"/studio/movies/{movie.movie_id}/releases/{release_id}/download"
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Movie asset storage is temporarily unavailable"
    }
