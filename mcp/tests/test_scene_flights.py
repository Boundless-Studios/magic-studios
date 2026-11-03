"""Scene-flight tools against a scripted transport."""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest
from magic_video_studio_mcp import tools
from magic_video_studio_mcp.client import StudioApiError
from test_tools import _client


def _flight(revision=1, legs=None, chain=None):
    return {"flight_id": "f", "revision": revision, "legs": legs or [], "chain": chain}


async def test_server_registers_scene_flight_tools():
    from magic_video_studio_mcp import server

    names = {tool.name for tool in await server.mcp.list_tools()}
    assert {
        "create_scene_flight", "get_scene_flight", "add_scene_flight_image", "set_scene_flight_legs",
        "generate_scene_flight_take", "generate_scene_flight_leg", "select_scene_flight_take",
        "wait_for_scene_flight", "chain_scene_flight", "download_scene_flight_video",
    } <= names


async def test_image_from_path_is_sent_as_base64(tmp_path):
    source = tmp_path / "plate.webp"
    source.write_bytes(b"webp-bytes")
    client, transport = _client([httpx.Response(200, json={"image_key": "k", "revision": 2})])
    await tools.add_scene_flight_image(client, "f", str(source))
    assert transport.requests[0].url.path == "/studio/scene-flights/f/images"
    assert transport.body(0) == {
        "content_base64": base64.b64encode(b"webp-bytes").decode(), "content_type": "image/webp"}


async def test_image_needs_exactly_one_source():
    client, transport = _client([])
    with pytest.raises(ValueError, match="exactly one"):
        await tools.add_scene_flight_image(client, "f")
    assert transport.requests == []


async def test_legs_drop_view_fields_and_retry_once_on_a_stale_revision():
    leg = {"leg_id": "a", "origin_image_key": "o", "destination_image_key": "d", "prompt": "go",
           "takes": [{"take_id": "t"}], "selected_take_id": "t", "index": 0}
    client, transport = _client([
        httpx.Response(200, json=_flight(3)),
        httpx.Response(409, json={"detail": {"message": "stale", "current_revision": 4}}),
        httpx.Response(200, json=_flight(5)),
    ])
    result = await tools.set_scene_flight_legs(client, "f", [leg])
    assert result["revision"] == 5
    assert transport.body(1) == {"legs": [{"leg_id": "a", "origin_image_key": "o",
                                           "destination_image_key": "d", "prompt": "go"}],
                                 "expected_revision": 3}
    assert transport.body(2)["expected_revision"] == 4


async def test_active_take_conflict_is_not_retried():
    client, _ = _client([
        httpx.Response(200, json=_flight(3)),
        httpx.Response(409, json={"detail": "Leg already has an active take"}),
    ])
    with pytest.raises(StudioApiError):
        await tools.generate_scene_flight_take(client, "f", "a", "r1", seed=9)


async def test_wait_settles_and_reports_the_newest_failed_take():
    running = _flight(legs=[{"leg_id": "a", "takes": [{"take_id": "t1", "status": "running"}]}])
    failed = _flight(legs=[{"leg_id": "a", "takes": [
        {"take_id": "t0", "status": "succeeded"}, {"take_id": "t1", "status": "failed", "error": "oom"}]}])
    client, transport = _client([httpx.Response(200, json=running), httpx.Response(200, json=failed)])

    async def sleep(seconds):
        pass

    result = await tools.wait_for_scene_flight(client, "f", 60, 5, sleep=sleep, clock=lambda: 0.0)
    assert result["settled"] is True
    assert result["failed"] == ["take a/t1: oom"]
    assert [r.method for r in transport.requests] == ["GET", "GET"]


async def test_download_uses_the_selected_take_with_the_api_key(tmp_path):
    flight = _flight(legs=[{"leg_id": "a", "selected_take_id": "t2", "takes": []}])
    client, transport = _client([httpx.Response(200, json=flight), httpx.Response(200, content=b"mp4")])
    result = await tools.download_scene_flight_video(client, "f", str(tmp_path / "leg.mp4"), "a")
    assert (tmp_path / "leg.mp4").read_bytes() == b"mp4"
    assert result["sha256"] == hashlib.sha256(b"mp4").hexdigest()
    assert transport.requests[1].url.path == "/studio/scene-flights/f/legs/a/takes/t2/video"
    assert transport.requests[1].url.params["download"] == "true"


async def test_one_call_leg_creates_uploads_appends_and_generates(tmp_path):
    origin, destination = tmp_path / "a.png", tmp_path / "b.png"
    origin.write_bytes(b"a")
    destination.write_bytes(b"b")
    existing = {"leg_id": "leg-1", "origin_image_key": "x", "destination_image_key": "y", "prompt": "p",
                "negative_prompt": "", "name": "Leg 1", "duration_seconds": 5.0, "takes": []}
    client, transport = _client([
        httpx.Response(200, json={"image_key": "ka", "revision": 2}),
        httpx.Response(200, json={"image_key": "kb", "revision": 3}),
        httpx.Response(200, json=_flight(3, [existing])),
        httpx.Response(200, json=_flight(3, [existing])),
        httpx.Response(200, json=_flight(4)),
        httpx.Response(200, json=_flight(4)),
        httpx.Response(200, json=_flight(5)),
    ])
    result = await tools.generate_scene_flight_leg(
        client, str(origin), str(destination), origin_name="Harrowfen", destination_name="Miretrace",
        flight_id="f", seed=11)
    assert result["leg_id"] == "leg-2"
    legs = json.loads(transport.requests[4].content)["legs"]
    assert [leg["leg_id"] for leg in legs] == ["leg-1", "leg-2"]
    assert legs[1]["origin_image_key"] == "ka" and legs[1]["destination_image_key"] == "kb"
    assert "takes" not in legs[0]
    generate = json.loads(transport.requests[6].content)
    assert generate["seed"] == 11 and generate["expected_revision"] == 4
    assert transport.requests[6].url.path == "/studio/scene-flights/f/legs/leg-2/takes"
