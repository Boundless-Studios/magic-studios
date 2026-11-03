import base64
import shutil
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from magic_video_editor.flights.routes import create_scene_flight_router
from magic_video_editor.flights.service import SceneFlightService
from magic_video_editor.local_studio import LocalAssetStore
from magic_video_editor.movies.local import LocalMoviePool
from magic_video_editor.movies.media import MovieMedia
from magic_video_editor.movies.routes import create_movie_router
from magic_video_editor.movies.service import MovieService
from magic_video_editor.movies.store import MemoryMovieStore
from magic_video_editor.studio.routes import studio_actor_id


def image(color):
    out = BytesIO()
    Image.new("RGB", (1536, 864), color).save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


def client(available=True, pool=True):
    assets = LocalAssetStore()
    movies = MovieService(
        MemoryMovieStore(), assets, LocalMoviePool(assets) if pool else None,
        media=MovieMedia(assets), scene_flights_available=available,
    )
    app = FastAPI()
    app.include_router(create_movie_router(movies))
    app.include_router(create_scene_flight_router(SceneFlightService(movies)))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    return app, TestClient(app)


def test_flights_are_owner_scoped_and_kept_out_of_the_movie_list():
    app, http = client()
    flight = http.post("/studio/scene-flights", json={"name": "Fen"}).json()
    movie = http.post("/studio/movies", json={"name": "Film"}).json()
    assert [f["flight_id"] for f in http.get("/studio/scene-flights").json()] == [flight["flight_id"]]
    assert [m["movie_id"] for m in http.get("/studio/movies").json()] == [movie["movie_id"]]
    assert http.get(f"/studio/scene-flights/{movie['movie_id']}").status_code == 404
    app.dependency_overrides[studio_actor_id] = lambda: "other"
    assert http.get(f"/studio/scene-flights/{flight['flight_id']}").status_code == 404
    assert http.get("/studio/scene-flights").json() == []


def test_movie_routes_treat_a_flight_as_not_found():
    _, http = client()
    flight = http.post("/studio/scene-flights", json={"name": "Fen"}).json()
    base = f"/studio/movies/{flight['flight_id']}"
    rev = {"expected_revision": flight["revision"]}
    clip = {"shot_id": "leg", "take_id": "t", "in_seconds": 0, "out_seconds": 1}
    calls = [
        ("GET", "", None),
        ("GET", "/history", None),
        ("PUT", "/story", {**rev, "shots": []}),
        ("POST", "/build", {**rev, "request_id": "r"}),
        ("POST", "/generate", {**rev, "shot_ids": ["leg"], "request_id": "r"}),
        ("POST", "/reconcile", None),
        ("POST", "/uploads", {"content_type": "image/png"}),
        ("POST", "/shots/leg/select", {**rev, "take_id": "t"}),
        ("POST", "/shots/leg/takes/t/cancel", rev),
        ("PUT", "/cut", {**rev, "clips": [clip]}),
        ("POST", "/assemble", {**rev, "request_id": "r"}),
        ("POST", "/release", {**rev, "name": "Fen"}),
        ("GET", "/assets/outputs/movies/x.png", None),
        ("DELETE", "", {"confirm_name": "Fen"}),
    ]
    for method, suffix, body in calls:
        response = http.request(method, base + suffix, json=body)
        assert response.status_code == 404, (method, suffix, response.text)
    kept = http.get(f"/studio/scene-flights/{flight['flight_id']}").json()
    assert kept["revision"] == flight["revision"]


def test_flight_routes_treat_a_movie_as_not_found():
    _, http = client()
    movie = http.post("/studio/movies", json={"name": "Film"}).json()
    base = f"/studio/scene-flights/{movie['movie_id']}"
    rev = {"expected_revision": movie["revision"]}
    calls = [
        ("GET", "", None),
        ("POST", "/images", {"content_base64": image("red")}),
        ("PUT", "/legs", {**rev, "legs": []}),
        ("POST", "/legs/a/takes", {**rev, "request_id": "r"}),
        ("POST", "/legs/a/select", {**rev, "take_id": "t"}),
        ("POST", "/legs/a/takes/t/cancel", rev),
        ("GET", "/legs/a/takes/t/video", None),
        ("POST", "/chain", {**rev, "request_id": "r"}),
        ("GET", "/chain/video", None),
        ("GET", "/assets/outputs/movies/x.png", None),
        ("DELETE", "", {"confirm_name": "Film"}),
    ]
    for method, suffix, body in calls:
        response = http.request(method, base + suffix, json=body)
        assert response.status_code == 404, (method, suffix, response.text)
    kept = http.get(f"/studio/movies/{movie['movie_id']}").json()
    assert kept["revision"] == movie["revision"]


def test_capabilities_report_the_recipe_and_availability():
    _, http = client(available=False)
    caps = http.get("/studio/scene-flights/capabilities").json()
    assert caps["generation_available"] is False
    assert (caps["width"], caps["height"], caps["fps"], caps["steps"]) == (768, 448, 24, 20)
    assert (caps["min_duration_seconds"], caps["max_duration_seconds"]) == (5.0, 15.0)


def test_invalid_images_and_legs_are_rejected():
    _, http = client()
    id = http.post("/studio/scene-flights", json={"name": "Fen"}).json()["flight_id"]
    assert http.post(f"/studio/scene-flights/{id}/images", json={"content_base64": "%%%"}).status_code == 422
    bad = base64.b64encode(b"not an image").decode()
    assert http.post(f"/studio/scene-flights/{id}/images", json={"content_base64": bad}).status_code == 422
    a = http.post(f"/studio/scene-flights/{id}/images", json={"content_base64": image("green")}).json()
    b = http.post(f"/studio/scene-flights/{id}/images", json={"content_base64": image("grey")}).json()
    leg = {"leg_id": "a", "origin_image_key": a["image_key"], "destination_image_key": b["image_key"]}
    # Neither a prompt nor place names.
    assert http.put(f"/studio/scene-flights/{id}/legs", json={"expected_revision": b["revision"], "legs": [leg]}).status_code == 422
    # Below H3's trained 124-frame floor.
    short = {**leg, "prompt": "go", "duration_seconds": 4}
    assert http.put(f"/studio/scene-flights/{id}/legs", json={"expected_revision": b["revision"], "legs": [short]}).status_code == 422
    stale = http.put(f"/studio/scene-flights/{id}/legs", json={"expected_revision": 1, "legs": [{**leg, "prompt": "go"}]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_revision"] == b["revision"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_leg_generates_lands_on_both_pins_and_downloads():
    _, http = client()
    id = http.post("/studio/scene-flights", json={"name": "Fen"}).json()["flight_id"]
    a = http.post(f"/studio/scene-flights/{id}/images", json={"content_base64": image((40, 120, 60))}).json()
    b = http.post(f"/studio/scene-flights/{id}/images", json={"content_base64": image((200, 200, 210))}).json()
    flight = http.put(f"/studio/scene-flights/{id}/legs", json={"expected_revision": b["revision"], "legs": [{
        "leg_id": "a", "origin_image_key": a["image_key"], "destination_image_key": b["image_key"],
        "origin_name": "Harrowfen", "destination_name": "Miretrace", "negative_prompt": "fog banks",
    }]}).json()
    assert flight["legs"][0]["name"] == "Harrowfen to Miretrace"
    flight = http.post(f"/studio/scene-flights/{id}/legs/a/takes", json={"expected_revision": flight["revision"], "request_id": "r1"}).json()
    for _ in range(4):
        flight = http.get(f"/studio/scene-flights/{id}").json()
        if flight["legs"][0]["takes"][0]["status"] == "succeeded":
            break
    take = flight["legs"][0]["takes"][0]
    assert take["status"] == "succeeded", take
    assert "Avoid: fog banks." in take["prompt"]
    assert take["first_frame_diff"] < 0.02
    assert take["last_frame_diff"] < 0.02
    assert 5.0 < take["duration_seconds"] < 5.3
    assert flight["legs"][0]["selected_take_id"] == take["take_id"]
    video = http.get(f"/studio/scene-flights/{id}/legs/a/takes/{take['take_id']}/video?download=true")
    assert video.status_code == 200
    assert video.headers["content-type"] == "video/mp4"
    assert "attachment" in video.headers["content-disposition"]
    assert http.get(f"/studio/scene-flights/{id}/legs/a/takes/nope/video").status_code == 404
    chained = http.post(f"/studio/scene-flights/{id}/chain", json={"expected_revision": flight["revision"], "request_id": "c1"}).json()
    assert chained["chain"]["status"] == "succeeded"
    assert http.get(f"/studio/scene-flights/{id}/chain/video").status_code == 200


def test_generation_without_a_pool_is_unavailable():
    _, http = client(pool=False)
    id = http.post("/studio/scene-flights", json={"name": "Fen"}).json()["flight_id"]
    a = http.post(f"/studio/scene-flights/{id}/images", json={"content_base64": image("green")}).json()
    b = http.post(f"/studio/scene-flights/{id}/images", json={"content_base64": image("grey")}).json()
    flight = http.put(f"/studio/scene-flights/{id}/legs", json={"expected_revision": b["revision"], "legs": [{
        "leg_id": "a", "origin_image_key": a["image_key"], "destination_image_key": b["image_key"], "prompt": "go"}]}).json()
    assert flight["generation_available"] is False
    response = http.post(f"/studio/scene-flights/{id}/legs/a/takes", json={"expected_revision": flight["revision"], "request_id": "r"})
    assert response.status_code == 503


def test_a_storage_outage_while_streaming_a_flight_image_is_503():
    from magic_video_editor.contracts import AssetStorageUnavailable

    assets = LocalAssetStore()
    movies = MovieService(MemoryMovieStore(), assets, media=MovieMedia(assets))
    app = FastAPI()
    app.include_router(create_scene_flight_router(SceneFlightService(movies)))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    http = TestClient(app)
    id = http.post("/studio/scene-flights", json={"name": "Fen"}).json()["flight_id"]
    key = http.post(f"/studio/scene-flights/{id}/images", json={"content_base64": image("green")}).json()["image_key"]
    assert http.get(f"/studio/scene-flights/{id}/assets/{key}").status_code == 200

    async def unreachable(_key):
        raise AssetStorageUnavailable("bucket unreachable")

    assets.describe = unreachable
    response = http.get(f"/studio/scene-flights/{id}/assets/{key}")
    assert response.status_code == 503
    assert response.json() == {"detail": "Movie asset storage is temporarily unavailable"}
