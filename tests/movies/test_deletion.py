"""Deleting a movie (or scene flight) removes its takes, cuts, releases and every
stored file, cancels its GPU work, and frees its storage."""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_video_editor.flights.routes import create_scene_flight_router
from magic_video_editor.flights.service import SceneFlightService
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.models import Shot, Take
from magic_video_editor.movies.routes import create_movie_router
from magic_video_editor.movies.service import MovieService
from magic_video_editor.movies.store import MemoryMovieStore
from magic_video_editor.studio.routes import studio_actor_id


class _Pool:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def submit(self, request):
        return None

    async def get(self, job_id):
        return SimpleNamespace(status="processing")

    async def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True


def _client(service: MovieService, owner: str) -> TestClient:
    app = FastAPI()
    app.include_router(create_movie_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: owner
    return TestClient(app)


def test_deleting_a_movie_removes_its_files_ledger_and_running_work() -> None:
    pool = _Pool()
    service = MovieService(MemoryMovieStore(), MemoryAssetStore(), pool)

    async def setup():
        movie = await service.create("owner", "Heist")
        kept = await service.create("owner", "Sequel")
        take_key = f"outputs/movies/{movie.movie_id}/takes/t.mp4"
        upload_key = f"uploads/movies/{movie.movie_id}/u"
        for key in (take_key, upload_key, f"outputs/movies/{kept.movie_id}/takes/k.mp4"):
            await service.assets.write_once(key, b"bytes", "video/mp4")
        await service.store.account_asset(movie.movie_id, take_key, 5)
        movie.shots = [Shot(shot_id="a", name="A", prompt="p", takes=[Take(
            take_id="t", request_id="r", job_id="job-running", prompt="p", asset_key=take_key,
            duration_seconds=2, status="running")])]
        await service.save(movie, movie.revision)
        return movie, kept

    movie, kept = asyncio.run(setup())
    route = f"/studio/movies/{movie.movie_id}"
    assert asyncio.run(service.store.stored_bytes("owner")) == 5

    assert _client(service, "owner").request("DELETE", route, json={"confirm_name": "heist"}).status_code == 422
    assert _client(service, "other").request("DELETE", route, json={"confirm_name": "Heist"}).status_code == 404
    deleted = _client(service, "owner").request("DELETE", route, json={"confirm_name": "Heist"})

    assert deleted.status_code == 204
    assert pool.cancelled == ["job-running"]
    assert _client(service, "owner").get(route).status_code == 404
    assert [key for key in service.assets.assets if movie.movie_id in key] == []
    assert asyncio.run(service.store.stored_bytes("owner")) == 0
    assert f"outputs/movies/{kept.movie_id}/takes/k.mp4" in service.assets.assets


def test_a_scene_flight_is_deleted_through_its_own_route() -> None:
    movies = MovieService(MemoryMovieStore(), MemoryAssetStore())
    flights = SceneFlightService(movies)
    flight = asyncio.run(flights.create("owner", "Canyon"))
    app = FastAPI()
    app.include_router(create_scene_flight_router(flights))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    client = TestClient(app)
    route = f"/studio/scene-flights/{flight['flight_id']}"

    assert client.request("DELETE", route, json={"confirm_name": "Canyon"}).status_code == 204
    assert client.get(route).status_code == 404
