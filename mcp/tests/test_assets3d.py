"""3D engine stage tools against a scripted transport."""

from __future__ import annotations

import json

import httpx
import pytest
from magic_video_studio_mcp import tools
from magic_video_studio_mcp.client import StudioApiError, StudioClient

NOT_QUALIFIED = "not available yet: the 3D engine profile is not qualified"


class Script(httpx.MockTransport):
    def __init__(self, responses):
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        super().__init__(self._handle)

    def _handle(self, request):
        self.requests.append(request)
        return self._responses.pop(0)

    def body(self, index):
        return json.loads(self.requests[index].content)


def _client(responses):
    transport = Script(responses)
    return StudioClient(base_url="https://studio.test", api_key="mvs_" + "a" * 32, transport=transport), transport


def _caps(available: bool, reason: str | None = NOT_QUALIFIED):
    entry = {"available": True, "reason": None, "worker_id": "w"} if available else {"available": False, "reason": reason}
    return {"account_tier": "invited", "stages": {"mesh": entry, "clean": entry}}


async def test_server_registers_3d_tools():
    from magic_video_studio_mcp import server

    names = {tool.name for tool in await server.mcp.list_tools()}
    assert {"get_3d_capabilities", "generate_3d_mesh", "clean_3d_mesh"} <= names


async def test_capabilities_pass_the_servers_reason_through():
    client, transport = _client([httpx.Response(200, json=_caps(False))])
    caps = await tools.get_3d_capabilities(client)
    assert caps["stages"]["mesh"] == {"available": False, "reason": NOT_QUALIFIED}
    assert transport.requests[0].url.path == "/studio/3d/capabilities"
    await client.aclose()


@pytest.mark.parametrize("call", ["mesh", "clean"])
async def test_an_unavailable_stage_reports_why_and_submits_nothing(call):
    client, transport = _client([httpx.Response(200, json=_caps(False))])
    if call == "mesh":
        result = await tools.generate_3d_mesh(client, "a-1", resolution=1024)
    else:
        result = await tools.clean_3d_mesh(client, "a-1", "m-1")
    assert result == {"available": False, "stage": call, "reason": NOT_QUALIFIED}
    # One capabilities read, and no stage request.
    assert [r.url.path for r in transport.requests] == ["/studio/3d/capabilities"]
    await client.aclose()


async def test_a_stage_that_closes_between_calls_still_reports_its_reason():
    refused = httpx.Response(409, json={"detail": {"code": "stage_unavailable", "message": NOT_QUALIFIED}})
    client, transport = _client([httpx.Response(200, json=_caps(True)), refused])
    result = await tools.generate_3d_mesh(client, "a-1")
    assert result == {"available": False, "stage": "mesh", "reason": NOT_QUALIFIED}
    await client.aclose()


async def test_generate_and_clean_send_the_stage_request():
    operation = {"request_id": "r-1", "status": "running", "operation_id": "op"}
    asset = {"asset_id": "a-1", "revision": 5, "operations": [operation]}
    client, transport = _client([
        httpx.Response(200, json=_caps(True)), httpx.Response(200, json=asset),
        httpx.Response(200, json=_caps(True)), httpx.Response(200, json={**asset, "operations": []}),
    ])
    result = await tools.generate_3d_mesh(client, "a-1", resolution=512, seed=7, request_id="r-1")
    assert transport.requests[1].method == "POST" and transport.requests[1].url.path == "/studio/3d/a-1/stages/mesh"
    assert transport.body(1) == {"request_id": "r-1", "settings": {"resolution": 512, "seed": 7}}
    assert result == {"available": True, "stage": "mesh", "operation": operation, "asset_id": "a-1", "revision": 5}
    await tools.clean_3d_mesh(client, "a-1", "m-1", target_triangles=20_000)
    body = transport.body(3)
    assert transport.requests[3].url.path == "/studio/3d/a-1/stages/clean"
    assert body["parent_id"] == "m-1" and body["settings"] == {"target_triangles": 20_000}
    assert body["request_id"]  # a fresh id by default
    await client.aclose()


async def test_other_errors_are_not_hidden():
    client, transport = _client([
        httpx.Response(200, json=_caps(True)),
        httpx.Response(422, json={"detail": "resolution must be one of 512, 1024."}),
    ])
    with pytest.raises(StudioApiError) as error:
        await tools.generate_3d_mesh(client, "a-1", resolution=768)
    assert error.value.status_code == 422
    await client.aclose()
