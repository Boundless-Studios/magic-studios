import pytest
from fastapi.testclient import TestClient

from magic_video_editor.local_studio import create_local_studio_app


def test_local_movie_flow_uses_real_video_without_gpu_spend():
    with TestClient(create_local_studio_app()) as client:
        capabilities = client.get("/studio/movies/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["generation_available"] is True
        movie = client.post("/studio/movies", json={"name": "Local film"}).json()
        id = movie["movie_id"]
        movie = client.put(
            f"/studio/movies/{id}/story",
            json={
                "expected_revision": movie["revision"],
                "brief": "Blue shot",
                "style": "painted",
                "shots": [
                    {
                        "shot_id": "one",
                        "name": "One",
                        "prompt": "Pan camera",
                        "opening_prompt": "Blue sky",
                        "duration_seconds": 1.5,
                    }
                ],
                "batch_size": 1,
            },
        ).json()
        movie = client.post(
            f"/studio/movies/{id}/shots/one/compose",
            json={"expected_revision": movie["revision"], "request_id": "composition"},
        ).json()
        assert movie["shots"][0]["opening_asset_key"]
        movie = client.post(
            f"/studio/movies/{id}/shots/one/approve",
            json={"expected_revision": movie["revision"]},
        ).json()
        movie = client.post(
            f"/studio/movies/{id}/generate",
            json={
                "expected_revision": movie["revision"],
                "request_id": "generate",
                "shot_ids": ["one"],
            },
        ).json()
        movie = client.post(f"/studio/movies/{id}/reconcile").json()
        take = movie["shots"][0]["takes"][0]
        assert take["status"] == "succeeded"
        response = client.get(f"/studio/movies/{id}/assets/{take['asset_key']}")
        assert response.status_code == 200
        assert b"ftyp" in response.content[:30]


@pytest.mark.parametrize("content_type", ["audio/wav", "audio/mpeg"])
def test_frozen_audio_scoped_playback_has_audio_mime_and_owner_isolation(content_type):
    import io
    import wave

    from fastapi import FastAPI

    from magic_video_editor.local_studio import create_local_studio_runtime
    from magic_video_editor.movies.routes import create_movie_router
    from magic_video_editor.studio.routes import studio_actor_id

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 8000)
    content = buffer.getvalue()
    if content_type == "audio/mpeg":
        import subprocess

        content = subprocess.check_output(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "wav",
                "-i",
                "pipe:0",
                "-f",
                "mp3",
                "pipe:1",
            ],
            input=content,
        )
    # Full local route follows scoped redirect through the local asset endpoint.
    with TestClient(create_local_studio_app()) as client:
        movie = client.post("/studio/movies", json={"name": "Audio film"}).json()
        base = f"/studio/movies/{movie['movie_id']}"
        grant = client.post(
            base + "/uploads", json={"content_type": content_type}
        ).json()
        assert (
            client.put(
                grant["upload_url"],
                content=content,
                headers={"Content-Type": content_type},
            ).status_code
            == 204
        )
        response = client.put(
            base + "/soundtracks",
            json={
                "expected_revision": grant["revision"],
                "tracks": [{"asset_key": grant["asset_key"]}],
            },
        )
        assert response.status_code == 200
        key = response.json()["soundtracks"][0]["asset_key"]
        playback = client.get(base + "/assets/" + key)
        assert playback.status_code == 200
        assert playback.content == content
        assert playback.headers["content-type"].startswith(content_type)
        other = client.post("/studio/movies", json={"name": "Other"}).json()
        assert (
            client.get(f"/studio/movies/{other['movie_id']}/assets/{key}").status_code
            == 404
        )

    # Production owner dependency must also reject a different actor.
    runtime = create_local_studio_runtime()
    app = FastAPI()
    app.include_router(create_movie_router(runtime.movie_service))
    app.dependency_overrides[studio_actor_id] = lambda: "first"
    with TestClient(app) as client:
        movie = client.post("/studio/movies", json={"name": "Owned"}).json()
        app.dependency_overrides[studio_actor_id] = lambda: "second"
        assert (
            client.get(f"/studio/movies/{movie['movie_id']}/assets/{key}").status_code
            == 404
        )
