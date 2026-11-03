import asyncio
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_video_editor import asset_delivery
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.models import Release
from magic_video_editor.movies.routes import create_movie_router
from magic_video_editor.movies.service import MovieService
from magic_video_editor.movies.store import MemoryMovieStore
from magic_video_editor.studio.routes import studio_actor_id


def test_movie_files_stream_to_their_owner_only_and_support_seeking(monkeypatch):
    # Small chunks so a whole-file read crosses several storage reads.
    monkeypatch.setattr(asset_delivery, "CHUNK_BYTES", 3)
    service = MovieService(MemoryMovieStore(), MemoryAssetStore())
    release_id = str(uuid4())

    async def setup():
        movie = await service.create("owner", "Film")
        key = f"outputs/movies/{movie.movie_id}/takes/take.mp4"
        movie.upload_keys = [key]
        movie.releases = [Release(release_id=release_id, name="Frozen", asset_key=key,
                                  cut_digest="frozen", source_revision=1)]
        await service.assets.write_once(key, b"0123456789", "video/mp4")
        await service.save(movie, movie.revision)
        return movie, key

    movie, key = asyncio.run(setup())
    app = FastAPI()
    app.include_router(create_movie_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    client = TestClient(app)
    route = f"/studio/movies/{movie.movie_id}/assets/{key}"

    response = client.get(route)
    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert response.headers["content-disposition"] == "inline"
    # Older clients' `?proxy=true` still works; nothing redirects to storage.
    assert client.get(route + "?proxy=true", follow_redirects=False).status_code == 200

    ranged = client.get(route, headers={"Range": "bytes=2-5"})
    assert ranged.status_code == 206
    assert ranged.content == b"2345"
    assert ranged.headers["content-range"] == "bytes 2-5/10"
    assert client.get(route, headers={"Range": "bytes=-3"}).content == b"789"
    assert client.get(route, headers={"Range": "bytes=7-"}).content == b"789"
    unsatisfiable = client.get(route, headers={"Range": "bytes=20-30"})
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["content-range"] == "bytes */10"

    release_route = f"/studio/movies/{movie.movie_id}/releases/{release_id}/download"
    release = client.get(release_route, follow_redirects=False)
    assert release.status_code == 200 and release.content == b"0123456789"
    assert release.headers["content-disposition"].startswith(
        f'attachment; filename="movie-{release_id}.mp4"'
    )
    assert client.get(route.replace("take.mp4", "unowned.mp4")).status_code == 404

    app.dependency_overrides[studio_actor_id] = lambda: "other"
    assert client.get(route).status_code == 404
    assert client.get(release_route).status_code == 404
